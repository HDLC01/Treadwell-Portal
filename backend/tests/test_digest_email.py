"""The morning digest email: what it renders, and what it refuses to send.

The proposal tool decides who and what; this end owns the document. So these tests
cover the two things that can go wrong here — an email that says the wrong thing
(unescaped input, a missing sentence, a link to nowhere) and an email that shouldn't
have gone out at all.
"""
import email_sender as es
import main
from fastapi.testclient import TestClient

client = TestClient(main.app)
HDRS = {"X-Service-Token": "test-token"}


def _capture(monkeypatch):
    box = {}

    def fake_send(to, subject, html, headers=None, reply_to=None, attachments=None):
        box.update(to=to, subject=subject, html=html, headers=headers or {},
                   reply_to=reply_to)
        return True

    monkeypatch.setattr(es, "_send", fake_send)
    return box


def item(**kw):
    base = {"proposal_id": "p1", "project_name": "Oak Grove Church",
            "customer": "Dave Miller", "stage": "Sent, not opened", "total": 41500.0,
            "reason": "Nobody has followed up and it's been eleven days — worth a call.",
            "score": 71, "streak": 1, "facts": ["sent 11 days ago"]}
    base.update(kw)
    return base


def link(pid):
    return f"https://proposals.wetreadwell.com/portal.html?open={pid}"


# ── the email ───────────────────────────────────────────────────────────────
def test_it_lists_the_project_the_customer_the_value_and_the_sentence(monkeypatch):
    box = _capture(monkeypatch)
    assert es.send_digest("kyle@wetreadwell.com", [item()],
                          name="Kyle", staff_link=link) is True
    html = box["html"]
    assert "Oak Grove Church" in html
    assert "Dave Miller" in html
    assert "$41,500" in html
    assert "worth a call" in html
    assert "Morning Kyle," in html


def test_the_project_name_is_the_link_to_the_crm(monkeypatch):
    """An estimator reading this on a phone taps the name — not a button five rows
    down."""
    box = _capture(monkeypatch)
    es.send_digest("kyle@wetreadwell.com", [item()], staff_link=link)
    assert f'href="{link("p1")}"' in box["html"]


def test_the_subject_counts_what_is_in_it(monkeypatch):
    box = _capture(monkeypatch)
    es.send_digest("k@w.com", [item()], staff_link=link)
    assert box["subject"] == "1 proposal to follow up today"
    es.send_digest("k@w.com", [item(), item(proposal_id="p2")], staff_link=link)
    assert box["subject"] == "2 proposals to follow up today"


def test_a_repeat_says_so_in_words(monkeypatch):
    """"3rd morning running" is the difference between a nudge and a duplicate email
    nobody trusts."""
    box = _capture(monkeypatch)
    es.send_digest("k@w.com", [item(streak=3)], staff_link=link)
    assert "3rd morning running" in box["html"]
    box2 = _capture(monkeypatch)
    es.send_digest("k@w.com", [item(streak=2)], staff_link=link)
    assert "again today" in box2["html"]
    box3 = _capture(monkeypatch)
    es.send_digest("k@w.com", [item(streak=1)], staff_link=link)
    assert "morning running" not in box3["html"] and "again today" not in box3["html"]


def test_a_truncated_list_says_how_many_it_left_out(monkeypatch):
    box = _capture(monkeypatch)
    es.send_digest("k@w.com", [item(and_more=4)], staff_link=link)
    assert "and 4 more" in box["html"]


def test_it_tells_the_estimator_how_to_get_off_tomorrow_list(monkeypatch):
    box = _capture(monkeypatch)
    es.send_digest("k@w.com", [item()], staff_link=link)
    assert "Logging a call" in box["html"]


def test_nothing_to_chase_sends_nothing(monkeypatch):
    """An empty digest every morning is how a daily email becomes one people filter
    away — and then the one that matters goes unread too."""
    box = _capture(monkeypatch)
    assert es.send_digest("k@w.com", [], staff_link=link) is False
    assert box == {}


def test_a_missing_value_or_customer_simply_is_not_shown(monkeypatch):
    box = _capture(monkeypatch)
    es.send_digest("k@w.com", [item(total=None, customer="", stage="")], staff_link=link)
    assert "$" not in box["html"].split("TREADWELL")[0]     # excludes the signature
    assert "Oak Grove Church" in box["html"]


def test_a_project_name_with_html_in_it_is_escaped(monkeypatch):
    """Project names come from an estimator typing into an intake form, and the
    customer name can come from an inbound email. Neither is markup."""
    box = _capture(monkeypatch)
    es.send_digest("k@w.com", [item(project_name="<script>bad()</script>",
                                    customer="<b>x</b>",
                                    reason="<img src=x onerror=y>")], staff_link=link)
    assert "<script>" not in box["html"]
    assert "&lt;script&gt;" in box["html"]
    assert "onerror" not in box["html"] or "&lt;img" in box["html"]


def test_it_greets_without_a_name_too(monkeypatch):
    box = _capture(monkeypatch)
    es.send_digest("k@w.com", [item()], staff_link=link)
    assert "Morning," in box["html"]


def test_it_threads_apart_from_every_proposal(monkeypatch):
    """Threading the digest onto a proposal's conversation would bury the customer's
    actual messages under a daily email — and there is no single proposal it belongs
    to anyway."""
    box = _capture(monkeypatch)
    es.send_digest("kyle@wetreadwell.com", [item()], staff_link=link)
    ref = box["headers"].get("References", "")
    assert "treadwell-digest." in ref
    assert "tw-proposal." not in ref


def test_two_estimators_get_different_threads(monkeypatch):
    box = _capture(monkeypatch)
    es.send_digest("kyle@wetreadwell.com", [item()], staff_link=link)
    a = box["headers"]["References"]
    es.send_digest("troy@wetreadwell.com", [item()], staff_link=link)
    assert box["headers"]["References"] != a


def test_the_same_estimator_keeps_one_thread(monkeypatch):
    box = _capture(monkeypatch)
    es.send_digest("Kyle@WeTreadwell.com", [item()], staff_link=link)
    a = box["headers"]["References"]
    es.send_digest("kyle@wetreadwell.com", [item()], staff_link=link)
    assert box["headers"]["References"] == a


# ── the endpoint ────────────────────────────────────────────────────────────
def _admin(monkeypatch, sent):
    monkeypatch.setattr(main, "_admin_ok", lambda r: True)
    monkeypatch.setattr(main.email_sender, "send_digest",
                        lambda email, items, **kw: sent.append((email, items, kw)) or True)


def test_the_endpoint_sends_for_a_valid_estimator(monkeypatch):
    sent = []
    _admin(monkeypatch, sent)
    r = client.post("/api/admin/send-digest", headers=HDRS,
                    json={"estimator_email": " Kyle@WeTreadwell.com ", "items": [item()]})
    assert r.status_code == 200 and r.json()["sent"] is True
    assert sent[0][0] == "kyle@wetreadwell.com"
    assert sent[0][2]["staff_link"] is main._staff_link


def test_it_requires_the_service_token(monkeypatch):
    monkeypatch.setattr(main, "_admin_ok", lambda r: False)
    r = client.post("/api/admin/send-digest", json={"estimator_email": "k@w.com", "items": []})
    assert r.status_code == 401


def test_a_bad_address_is_refused(monkeypatch):
    sent = []
    _admin(monkeypatch, sent)
    for bad in ("", "   ", "not-an-email"):
        r = client.post("/api/admin/send-digest", headers=HDRS,
                        json={"estimator_email": bad, "items": [item()]})
        assert r.status_code == 400, bad
    assert sent == []


def test_items_that_are_not_a_list_are_refused(monkeypatch):
    sent = []
    _admin(monkeypatch, sent)
    r = client.post("/api/admin/send-digest", headers=HDRS,
                    json={"estimator_email": "k@w.com", "items": "five of them"})
    assert r.status_code == 400 and sent == []


def test_an_empty_list_is_a_no_op_not_an_error(monkeypatch):
    sent = []
    _admin(monkeypatch, sent)
    r = client.post("/api/admin/send-digest", headers=HDRS,
                    json={"estimator_email": "k@w.com", "items": []})
    assert r.status_code == 200 and r.json()["sent"] is False and sent == []


def test_the_endpoint_caps_the_list_too(monkeypatch):
    """Capped here as well as at the source: this must not be a way to send a
    hundred-row email, whatever the caller believes it is sending."""
    sent = []
    _admin(monkeypatch, sent)
    client.post("/api/admin/send-digest", headers=HDRS,
                json={"estimator_email": "k@w.com",
                      "items": [item(proposal_id=f"p{i}") for i in range(60)]})
    assert len(sent[0][1]) == 25


def test_junk_entries_are_dropped_before_rendering(monkeypatch):
    sent = []
    _admin(monkeypatch, sent)
    client.post("/api/admin/send-digest", headers=HDRS,
                json={"estimator_email": "k@w.com", "items": ["a string", None, item()]})
    assert len(sent[0][1]) == 1


def test_a_first_name_is_read_off_the_address():
    """portal_app is denied the profiles table by design, so the real name isn't
    reachable from here — and "Morning Kyle," off kyle@ beats no greeting."""
    assert main._estimator_name("kyle.loseke@wetreadwell.com") == "Kyle Loseke"
    assert main._estimator_name("troy@wetreadwell.com") == "Troy"
    assert main._estimator_name("") == ""
