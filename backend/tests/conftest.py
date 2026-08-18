import os

import pytest


# ── the thread-subject settings read ─────────────────────────────────────────
# customer_thread_subject() reads portal_settings on every project email. In a test there is no
# database, so each send sat on the connection pool for its full 30s timeout before falling back
# — one subset of the suite went from seconds to six minutes.
#
# Patched at `_thread_subject_template`, NOT at `db.get_settings`. Stubbing the db function
# looked tidier and broke test_any_other_read_failure_is_reported_not_swallowed, which exists
# precisely to make that read fail: an autouse fixture that reaches into shared infrastructure
# silently overrides the setup of every test that cares about it. This patch touches only the
# email path, and a test that wants a configured subject overrides it again.
#
# `_sent_template` is here for exactly the same reason and was added a day later: the "your
# proposal is ready" email became editable, so publishing now reads portal_settings once per
# recipient. That put test_email_content back where the thread subject had been — 30s per send,
# two minutes for the file. None is the shipped-copy fallback, which is what those tests assert.
@pytest.fixture(autouse=True)
def _shipped_email_wording(request, monkeypatch):
    # A test that INSPECTS one of these functions has to see the real one — `realwording` is its
    # opt-out. Without it, inspect.getsource(email_sender._sent_template) returned the lambda two
    # lines below, and test_the_template_read_is_cached failed looking for a cache in it. Third
    # time an autouse fixture reaching into a shared module has bitten this file; the other two
    # are documented above and below, and the pattern is always the same — patch narrowly, and
    # give the tests that own the thing a way out.
    if "realwording" in request.keywords:
        return
    import email_sender
    monkeypatch.setattr(email_sender, "_thread_subject_template",
                        lambda: email_sender.DEFAULT_THREAD_SUBJECT, raising=False)
    monkeypatch.setattr(email_sender, "_sent_template", lambda: None, raising=False)


# ── the board's two per-poll decoration reads ────────────────────────────────
# admin_pipeline reads views_by_proposal() and recipients_by_proposal() once per poll to say
# "2 recipients · 1 viewed". Both are guarded and return {} on failure, so in a test with no
# database they are CORRECT but slow: each waits out the full 30s connection-pool timeout
# before giving up, which turned the pipeline tests into minutes.
#
# Stubbed to "nothing recorded", which is what a database without the migration returns anyway
# — so the default here is also the degraded path, and a test that cares about the line supplies
# its own rows. Same reasoning as the thread subject above, and the same reason it is patched at
# the specific functions rather than at the connection.
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "realdb: keep the real db view/recipient/notify-roster helpers instead of the autouse "
        "stubs below")
    config.addinivalue_line(
        "markers",
        "realwording: keep the real email-wording readers instead of the autouse stubs above")


@pytest.fixture(autouse=True)
def _no_view_records(request, monkeypatch):
    # A file that TESTS these helpers has to see the real ones. Without the opt-out the stubs
    # below silently replaced the functions under test, and inspect.getsource() returned this
    # fixture's own lambda — an autouse fixture reaching into a shared module is the same trap
    # that broke test_any_other_read_failure_is_reported_not_swallowed earlier.
    if "realdb" in request.keywords:
        return
    import db
    monkeypatch.setattr(db, "views_by_proposal", lambda: {}, raising=False)
    monkeypatch.setattr(db, "recipients_by_proposal", lambda: {}, raising=False)
    # The per-proposal pair, for the same reason: record_view runs on the customer's page load
    # and list_views decorates the staff drawer. Both swallow their own failures, so without
    # these the suite is merely slow rather than red — which is the worse of the two, because
    # nobody investigates slow.
    monkeypatch.setattr(db, "record_view", lambda pid, email: None, raising=False)
    monkeypatch.setattr(db, "list_views", lambda pid: [], raising=False)
    # Stamped on every publish. It swallows its own failures too, so leaving it real costs the
    # publish fixtures a connection-pool timeout each — slow, not red, which is the worse of the
    # two for exactly the reason above.
    monkeypatch.setattr(db, "mark_last_sent", lambda pid: None, raising=False)


# ── the notification roster, read TWICE on every staff email ─────────────────
# `email_sender._resolve_notify` reads portal_notify_recipients and then this project's
# portal_notify_overrides, so every team notification makes two guarded reads. Both are correct
# with no database and both waited out the connection pool's full 30s timeout first, and this was
# by far the most expensive thing in the suite: 44 of the 52 stalls a whole-suite run made, which
# is 22 of its 26 minutes. A publish ends in a send-confirmation notify_team, so the two files
# that publish paid it per test — test_revisions.py 662s and test_creator_gets_notified.py 602s,
# the latter with one test that publishes four times in a loop and therefore stalled eight times.
#
# `[]` rather than raising because for this caller the two are already indistinguishable:
# resolve_notify_recipients() is handed configured=False either way and falls back to
# config.NOTIFY_EMAILS / DEPOSIT_NOTIFY_EMAILS, and for the overrides an exception and an empty
# list both leave adds/mutes empty. `staff_emails()` reads the same two and falls through to the
# same env lists on either. So this does not pick a different branch from the one the suite was
# already taking — it stops the suite paying a minute per send to arrive at it. Verified: with
# these reads failing instantly instead of slowly, all 816 tests still pass.
#
# Patched at the two db functions, for the reason this file keeps repeating. The files that own
# this behaviour install their own roster and therefore override these — test_inbound.py's
# staff_emails pair, test_chat_reaches_the_estimator.py (a FakeDB in sys.modules),
# test_notify_pick_applied.py, test_staff_reply_by_email.py — because a function-scoped autouse
# fixture runs before the test body. Nothing reads either function UNGUARDED under test:
# /api/admin/notify-recipients and /api/admin/proposal/{id}/notify-overrides are staging-smoke
# territory, per the convention stated at the top of test_customer_auth.py.
@pytest.fixture(autouse=True)
def _no_notify_roster(request, monkeypatch):
    if "realdb" in request.keywords:
        return
    import db
    monkeypatch.setattr(db, "list_notify_recipients", lambda: [], raising=False)
    monkeypatch.setattr(db, "list_notify_overrides", lambda pid: [], raising=False)


# ── the backstop: an unstubbed call costs a second, not thirty ────────────────
# Everything above is the actual fix, and each entry is at a named function and says why. This is
# only the net underneath it, for the NEXT call somebody forgets: psycopg_pool's default `timeout`
# is 30 seconds, so ONE unguarded-but-guarded read is 30 seconds per call, and the symptom is a
# slow suite rather than a red one. Nobody investigates slow — which is how each of the entries
# above cost minutes for weeks before anyone measured.
#
# It shortens the WAIT and nothing else. The call still runs, still reaches psycopg, still fails,
# and still fails the same way: `getconn` reads `self.timeout` per acquisition and raises
# PoolTimeout either way, so a guarded caller takes exactly the fallback it takes today and an
# unguarded one still 500s. db.get_settings in particular still raises SettingsUnreadable for a
# connection failure and still returns None for a missing table, which is what
# test_any_other_read_failure_is_reported_not_swallowed and its neighbour pin (they patch db.q1
# and never reach the pool at all).
#
# NOT a substitute for a stub, and deliberately not sized like one: a second times a few hundred
# calls is still minutes. --durations=25 in pytest.ini is how the next one gets spotted.
#
# One read legitimately relies on this rather than on a stub, and it is named here so the next
# person does not "tidy" it into the fixtures above: portal_page's db.get_proposal_by_token, hit
# once by test_invoice_delivery.py::test_assets_and_shell_always_revalidate. That test asks
# GET /p/{token} whether it carries a no-cache header; the landing page's click-recording read is
# incidental to it, and get_proposal_by_token is a primary lookup that a blanket autouse stub
# would quietly answer "not found" for on every future customer route. One second is the right
# price for it.
@pytest.fixture(autouse=True, scope="session")
def _pool_gives_up_quickly():
    import db

    real_pool = db.pool

    def impatient_pool():
        p = real_pool()
        p.timeout = 1.0
        return p

    db.pool = impatient_pool
    # Belt and braces for an environment where DATABASE_URL points somewhere routable but silent:
    # the pool deadline above caps how long a CALLER waits, while this caps how long the pool's
    # own background worker sits on a TCP handshake. Inert on the default localhost URL, where the
    # connection is refused immediately.
    os.environ.setdefault("PGCONNECT_TIMEOUT", "1")
    yield
    db.pool = real_pool
