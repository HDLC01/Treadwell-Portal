"""Recording that somebody followed the link in a notification email.

Hanz asked: *"Is it possible that when a customer opens that email thread or opens that
chatbox with the specific project in portal it is labeled as seen?"*

The chatbox half already worked — chat is the default view of `/p/{token}`, and loading that
page calls `mark_viewed`. The email half was not tracked at all: `/p/{token}` served
`index.html` and recorded nothing, and the Resend webhook drops every event that is not
`email.received`.

So this adds the reliable half of the email signal — a click on the per-proposal link — and
keeps it firmly away from "viewed". That separation is the whole point of the file:

  * `/p/{token}` serves BEFORE anyone signs in, and Outlook SafeLinks and mail scanners follow
    links on their own. A click is evidence about the EMAIL, not about a person reading a bid.
  * `mark_viewed` also writes `cycle_viewed_at`, which `followup_rules` uses to switch a
    customer from the not-opened reminder track to the opened one. Flipping status on a click
    would let a mail scanner silently change which emails a customer receives — a wrong fact
    propagating into outbound mail, which `followups-core.js` warns about in as many words.

What the signal is actually for: a proposal that has sat in Sent for a week is a different
problem depending on whether the email ever arrived. "They are still deciding" and "we have
the wrong address" looked identical on the board before this.
"""
import pytest

import db
import main


# ── the write ─────────────────────────────────────────────────────────────────
def _sql_of(monkeypatch, fn, *args):
    seen = {}
    monkeypatch.setattr(db, "execute", lambda sql, params=(): seen.update(sql=sql, params=params))
    fn(*args)
    return " ".join(seen["sql"].split()), seen["params"]


def test_a_click_records_first_and_latest(monkeypatch):
    sql, params = _sql_of(monkeypatch, db.mark_link_clicked, "prop-1")
    assert "link_clicked_at = coalesce(link_clicked_at, now())" in sql, "first click must not move"
    assert "last_link_clicked_at = now()" in sql
    assert params == ("prop-1",)


@pytest.mark.parametrize("forbidden", [
    "proposal_status",     # a click is not a view
    "cycle_viewed_at",     # this anchors the follow-up cadence
    "viewed_at",
    "last_viewed_at",
])
def test_a_click_never_touches_anything_that_means_viewed(monkeypatch, forbidden):
    """THE guarantee. If a click ever starts writing these, a mail scanner following a link
    changes the customer's reminder schedule and tells the estimator the bid was read."""
    sql, _ = _sql_of(monkeypatch, db.mark_link_clicked, "prop-1")
    assert forbidden not in sql, "mark_link_clicked writes %s" % forbidden


def test_a_click_does_not_bump_updated_at(monkeypatch):
    """`updated_at` feeds "last activity". A link checker hitting the URL should not shuffle a
    card to the top of a board sorted by activity."""
    sql, _ = _sql_of(monkeypatch, db.mark_link_clicked, "prop-1")
    assert "updated_at" not in sql


def test_mark_viewed_still_does_all_of_its_own_work(monkeypatch):
    """The complement: the real view path must be unchanged by any of this."""
    sql, _ = _sql_of(monkeypatch, db.mark_viewed, "prop-1")
    for expected in ("viewed_at = coalesce(viewed_at, now())", "last_viewed_at = now()",
                     "cycle_viewed_at", "proposal_status"):
        assert expected in sql


# ── the route ─────────────────────────────────────────────────────────────────
class _Req:
    def __init__(self, method="GET"):
        self.method = method


def test_opening_the_email_link_records_the_click(monkeypatch):
    calls = []
    monkeypatch.setattr(main.db, "get_proposal_by_token",
                        lambda t: {"proposal_id": "prop-9"} if t == "tok" else None)
    monkeypatch.setattr(main.db, "mark_link_clicked", lambda pid: calls.append(pid))
    main.portal_page("tok", _Req())
    assert calls == ["prop-9"]


def test_a_head_request_is_not_a_person(monkeypatch):
    """Starlette answers HEAD on every GET route, and HEAD is prefetchers and link checkers."""
    calls = []
    monkeypatch.setattr(main.db, "get_proposal_by_token", lambda t: {"proposal_id": "prop-9"})
    monkeypatch.setattr(main.db, "mark_link_clicked", lambda pid: calls.append(pid))
    main.portal_page("tok", _Req("HEAD"))
    assert calls == []


def test_an_unknown_token_records_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(main.db, "get_proposal_by_token", lambda t: None)
    monkeypatch.setattr(main.db, "mark_link_clicked", lambda pid: calls.append(pid))
    main.portal_page("nope", _Req())
    assert calls == []


def test_the_page_still_serves_when_the_database_is_unhappy(monkeypatch):
    """A timestamp is worth nothing next to the customer's proposal being reachable. A DB
    hiccup here must not take the page down."""
    monkeypatch.setattr(main.db, "get_proposal_by_token",
                        lambda t: (_ for _ in ()).throw(RuntimeError("db down")))
    resp = main.portal_page("tok", _Req())
    assert resp is not None
    assert str(getattr(resp, "path", "")).endswith("index.html")


def test_the_landing_page_still_does_not_authenticate_anyone(monkeypatch):
    """The click must not become a back door: this route reads a token and writes a timestamp,
    and must not hand out proposal data or a session."""
    monkeypatch.setattr(main.db, "get_proposal_by_token", lambda t: {"proposal_id": "p"})
    monkeypatch.setattr(main.db, "mark_link_clicked", lambda pid: None)
    resp = main.portal_page("tok", _Req())
    assert str(getattr(resp, "path", "")).endswith("index.html")
    assert not getattr(resp, "body", b""), "the landing page should be the static shell only"


# ── the row the staff board reads ─────────────────────────────────────────────
def test_the_pipeline_row_carries_the_click_fields():
    import inspect
    src = inspect.getsource(main)
    assert '"link_clicked_at": _iso(r.get("link_clicked_at"))' in src
    assert '"last_link_clicked_at": _iso(r.get("last_link_clicked_at"))' in src


def test_the_pipeline_query_actually_selects_the_click_columns(monkeypatch):
    """Building the response dict from `r.get("link_clicked_at")` is not enough on its own.

    This is the bug that shipped. The dict referenced the field and the test above passed, but
    the SELECT list never fetched the column — so every row reported null, the staff board
    silently never drew the hint, and nothing failed. Found only by clicking a real link on
    staging and noticing the field was present in the JSON and always empty.

    A dict key and a SELECT list are two separate places that have to agree, so both get
    asserted.

    Asserts the column is FETCHED and aliased, not the syntax used to fetch it. The original
    version required the literal `p.link_clicked_at`, which is exactly what a later fix had to
    stop doing: naming a column prod does not have yet 500s the only pipeline query and takes the
    whole staff board down until an owner runs the ALTER. Pinning the spelling would have made
    that fix look like a regression."""
    seen = {}
    monkeypatch.setattr(db, "qall", lambda sql, params=(): seen.update(sql=sql) or [])
    db.list_all_portal_proposals()
    sql = " ".join(seen["sql"].split())
    for col in ("link_clicked_at", "last_link_clicked_at"):
        assert col in sql, "the query does not fetch %s at all" % col
        assert "as %s" % col in sql or "p.%s" % col in sql, (
            "%s is mentioned but never lands under that name, so r.get() reads null forever" % col)


def test_the_columns_exist_in_the_schema():
    import pathlib
    sql = (pathlib.Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8")
    for col in ("link_clicked_at", "last_link_clicked_at"):
        assert ("add column if not exists %s timestamptz" % col) in sql, col


# ── has the customer ever come back to us? ────────────────────────────────────
def test_the_query_fetches_when_the_customer_last_replied(monkeypatch):
    """`last_message_at` is the newest message from EITHER side, so a proposal where we sent the
    last note looks identical to one the customer answered. The board has to tell "never
    responded" from "mid-conversation" because those call for opposite actions — chase versus
    answer — so it needs a customer-only timestamp.

    Both the SELECT and the response dict are asserted: adding the field to only one of them is
    the exact bug that shipped with link_clicked_at, where the key was present and always null.
    """
    seen = {}
    monkeypatch.setattr(db, "qall", lambda sql, params=(): seen.update(sql=sql) or [])
    db.list_all_portal_proposals()
    sql = " ".join(seen["sql"].split())
    assert "as customer_replied_at" in sql
    assert "q.author_kind = 'customer'" in sql, "the subquery is not restricted to the customer"

    import inspect
    assert '"customer_replied_at": _iso(r.get("customer_replied_at"))' in inspect.getsource(main)


def test_the_reply_signal_counts_a_status_card_answer_too(monkeypatch):
    """A customer who answered the status card ("still deciding, ask me in two weeks") HAS
    responded. Restricting this to msg_type='text' would file them under "never replied" and earn
    them a chasing email they had already pre-empted — so the subquery deliberately does not
    filter on msg_type, unlike the unread count next to it."""
    seen = {}
    monkeypatch.setattr(db, "qall", lambda sql, params=(): seen.update(sql=sql) or [])
    db.list_all_portal_proposals()
    sql = " ".join(seen["sql"].split())
    # Isolate the customer_replied_at subquery and check it carries no msg_type restriction.
    frag = sql[:sql.index("as customer_replied_at")]
    frag = frag[frag.rindex("(select max(q.created_at)"):]
    assert "msg_type" not in frag, (
        "customer_replied_at filters on msg_type; a status-card answer would not count as a reply")
