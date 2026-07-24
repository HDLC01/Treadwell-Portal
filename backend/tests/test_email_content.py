"""Customer email bodies: first-name greeting, the estimator's personal note in
the proposal-ready email, and the actual reply TEXT in the reply email (Will's
ask — content in the email, not just a portal button). Pure body-building tests:
monkeypatch _send to capture the HTML, no network.
"""
import email_sender as es


def _capture(monkeypatch):
    box = {}

    def fake_send(to, subject, html, headers=None, reply_to=None):
        box.update(to=to, subject=subject, html=html, reply_to=reply_to)
        return True

    monkeypatch.setattr(es, "_send", fake_send)
    return box


def test_first_name_helper():
    assert es._first_name("John Smith") == "John"
    assert es._first_name("  Mary  Jane  Watson ") == "Mary"
    assert es._first_name("") == ""
    assert es._first_name(None) == ""


def test_portal_link_greets_first_name_only(monkeypatch):
    box = _capture(monkeypatch)
    es.send_portal_link("c@x.com", "John Smith", "http://u", "Westport")
    assert "Hi John," in box["html"]
    assert "Smith" not in box["html"]          # last name dropped


def test_portal_link_shows_estimator_note(monkeypatch):
    box = _capture(monkeypatch)
    es.send_portal_link("c@x.com", "Jane Doe", "http://u", "Westport",
                        note="Thanks for the walkthrough — call me with questions.")
    assert "Thanks for the walkthrough" in box["html"]
    # blank/absent note adds nothing extra
    box2 = _capture(monkeypatch)
    es.send_portal_link("c@x.com", "Jane", "http://u", "Westport", note="   ")
    assert "border-left:3px solid #0ea5e9" not in box2["html"]


def test_reply_email_includes_reply_text_escaped(monkeypatch):
    box = _capture(monkeypatch)
    es.send_reply_notification("c@x.com", "http://u", "Westport",
                               message="Yes, we can start Monday. <b>x</b> & done")
    assert "Yes, we can start Monday." in box["html"]     # content shown, not just a button
    assert "&lt;b&gt;x&lt;/b&gt;" in box["html"]           # HTML-escaped (no injection)
    assert "&amp; done" in box["html"]


def test_signature_footer_address_then_tagline(monkeypatch):
    box = _capture(monkeypatch)
    es.send_portal_link("c@x.com", "Jane", "http://u", "Westport")
    html = box["html"]
    assert "1707 E. 123rd Ter, Olathe, KS 66061" in html
    assert "commercial epoxy" in html
    # address line comes BEFORE the tagline (Will's order)
    assert html.index("1707 E. 123rd Ter") < html.index("commercial epoxy")
    # footer is on the reply + deposit emails too (single _wrap choke-point)
    box2 = _capture(monkeypatch)
    es.send_deposit_request("c@x.com", "http://u", "Westport", amount=100.0)
    assert "1707 E. 123rd Ter, Olathe, KS 66061" in box2["html"]


def test_note_and_message_are_html_escaped(monkeypatch):
    box = _capture(monkeypatch)
    es.send_portal_link("c@x.com", "Jane", "http://u", "Westport",
                        note="<script>alert(1)</script>")
    assert "<script>" not in box["html"]
    assert "&lt;script&gt;" in box["html"]
