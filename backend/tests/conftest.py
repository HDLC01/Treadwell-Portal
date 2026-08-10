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
@pytest.fixture(autouse=True)
def _shipped_thread_subject(monkeypatch):
    import email_sender
    monkeypatch.setattr(email_sender, "_thread_subject_template",
                        lambda: email_sender.DEFAULT_THREAD_SUBJECT, raising=False)
