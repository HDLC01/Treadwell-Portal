"""POST /api/portal/{token}/viewed — "viewed" means the customer opened the proposal step.

Hanz, 2026-08-13, two reports that turned out to be one bug:

    "it should only move to viewed if they actually click view the proposal in the chatbox inside
     the portal. Not by clicking the portal link only. They need first to open the Status page
     under the Proposal Step"

    "I have resent the proposal again but it didnt move back to sent? Hanz Combo 2. So that we
     have the cadence if they have viewed the revision."

Marking used to happen on any authenticated `GET /api/portal/{token}`, which runs at portal boot
before the customer has navigated anywhere. So "Viewed" meant "their browser fetched the portal" —
and the re-send reset was real but invisible: `reset_for_revision` put the row back to 'sent', then
the open tab's 12-second poll saw `revision_no` change, refetched that GET, and the row was marked
viewed again with nobody having read anything. The phantom also re-stamped `cycle_viewed_at`, which
anchors the follow-up cadence, so the customer moved onto the "they opened it" reminder track off a
view that never happened.

THE REVISION GUARD IS THE POINT. Without it this endpoint recreates the same bug in a new shape: a
tab still displaying the previous revision fires on its next hash change, the row is back in 'sent',
and `mark_viewed` re-stamps the cadence anchor. A tab may only mark the revision it is showing.
"""
import pytest


@pytest.fixture
def client(monkeypatch):
    """TestClient with the auth gate and both write helpers stubbed.

    `mark_viewed` must be stubbed explicitly — conftest's autouse fixture covers `record_view` but
    not this one, and unstubbed it reaches for a database that is not there."""
    from fastapi.testclient import TestClient
    import main

    calls = {"marked": [], "recorded": []}
    row = {"proposal_id": "pid-1", "project_name": "Hanz Combo 2", "current_revision_no": 2}

    monkeypatch.setattr(main, "_require", lambda request, token: (row if token != "denied" else None))
    monkeypatch.setattr(main, "_session_email", lambda request: "dana@customer.com")
    monkeypatch.setattr(main.db, "mark_viewed", lambda pid: calls["marked"].append(pid))
    monkeypatch.setattr(main.db, "record_view",
                        lambda pid, email: calls["recorded"].append((pid, email)))

    tc = TestClient(main.app)
    tc.calls = calls
    tc.row = row
    tc.main = main
    tc.monkeypatch = monkeypatch
    return tc


def _post(client, body, token="tok"):
    return client.post(f"/api/portal/{token}/viewed", json=body)


# ── the happy path ───────────────────────────────────────────────────────────
def test_opening_the_current_revision_marks_it_viewed(client):
    r = _post(client, {"revision_no": 2})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "marked": True}
    assert client.calls["marked"] == ["pid-1"]
    assert client.calls["recorded"] == [("pid-1", "dana@customer.com")]


def test_the_recorded_reader_is_the_SESSION_not_the_body(client):
    """The per-contact "Viewed" chip in the staff drawer prints this name. Anything the client
    supplies would make it a claim rather than a record."""
    r = _post(client, {"revision_no": 2, "email": "attacker@elsewhere.com"})
    assert r.status_code == 200
    assert client.calls["recorded"] == [("pid-1", "dana@customer.com")]


# ── the guard that stops the phantom coming back ─────────────────────────────
def test_a_tab_showing_the_PREVIOUS_revision_marks_nothing(client):
    """THE INCIDENT, in its new shape. Staff re-send; the customer's open tab is still rendering
    revision 2 and fires on a hash change. The row is back in 'sent', so an unguarded mark would
    flip it straight to 'viewed' AND re-stamp cycle_viewed_at — the exact phantom that made the
    re-send look like it never happened."""
    client.row["current_revision_no"] = 3
    r = _post(client, {"revision_no": 2})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "marked": False}
    assert client.calls["marked"] == [] and client.calls["recorded"] == []


def test_a_stale_tab_gets_a_200_so_it_stops_asking(client):
    """Not an error: the client latches on the response. A 4xx would leave it retrying forever
    against a revision it will never hold — and it re-fires by itself once its poll delivers the
    new one, which is precisely when the customer IS looking at the new document."""
    client.row["current_revision_no"] = 3
    assert _post(client, {"revision_no": 2}).status_code == 200


def test_a_legacy_tab_cannot_mark_a_pinned_row(client):
    """A tab loaded before the row was ever revisioned sends null. That is a real value here, not
    'unknown', so it must not match revision 2."""
    r = _post(client, {"revision_no": None})
    assert r.json() == {"ok": True, "marked": False}
    assert client.calls["marked"] == []


def test_garbage_is_treated_as_no_revision(client):
    for bad in ("abc", {"nested": 1}, [1, 2], True):
        client.calls["marked"].clear()
        r = _post(client, {"revision_no": bad})
        assert r.status_code == 200, (bad, r.text)
        assert client.calls["marked"] == [], f"{bad!r} was accepted as revision 2"


def test_a_missing_body_key_marks_nothing_on_a_pinned_row(client):
    assert _post(client, {}).json() == {"ok": True, "marked": False}
    assert client.calls["marked"] == []


# ── legacy rows still work ───────────────────────────────────────────────────
def test_an_unrevisioned_row_is_marked_by_a_null_revision(client):
    """Rows predating revisions have `current_revision_no` null. null == null is a match, or those
    customers would never be recorded as having read anything."""
    client.row["current_revision_no"] = None
    r = _post(client, {"revision_no": None})
    assert r.json() == {"ok": True, "marked": True}
    assert client.calls["marked"] == ["pid-1"]


def test_a_pinned_tab_cannot_mark_an_unrevisioned_row(client):
    client.row["current_revision_no"] = None
    assert _post(client, {"revision_no": 2}).json() == {"ok": True, "marked": False}


# ── auth + failure shape ─────────────────────────────────────────────────────
def test_without_access_it_is_a_401_and_writes_nothing(client):
    r = _post(client, {"revision_no": 2}, token="denied")
    assert r.status_code == 401
    assert client.calls["marked"] == [] and client.calls["recorded"] == []


def test_a_database_failure_never_reaches_the_customer(client):
    """This used to run inside the customer's main GET, where a blip in `mark_viewed` — which is
    NOT self-guarded, unlike record_view — took the whole proposal page down. Moving it here
    removes that failure mode only if the endpoint actually absorbs it."""
    def boom(pid):
        raise RuntimeError("connection pool exhausted")

    client.monkeypatch.setattr(client.main.db, "mark_viewed", boom)
    r = _post(client, {"revision_no": 2})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "marked": False}
    assert client.calls["recorded"] == [], "a view was recorded against a status that never moved"


def test_marking_twice_is_harmless(client):
    """The client latches, but a reload or a second tab will repeat it. mark_viewed coalesces
    `viewed_at` and only stamps `cycle_viewed_at` from 'sent', so the endpoint needs no dedupe of
    its own — this pins that it doesn't grow one."""
    for _ in range(3):
        assert _post(client, {"revision_no": 2}).json()["marked"] is True
    assert client.calls["marked"] == ["pid-1"] * 3
