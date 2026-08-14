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
        "realdb: keep the real db view/recipient helpers instead of the autouse stubs below")
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
