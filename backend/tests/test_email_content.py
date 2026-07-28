"""Customer email bodies: first-name greeting, the estimator's personal note in
the proposal-ready email, and the actual reply TEXT in the reply email (Will's
ask — content in the email, not just a portal button). Pure body-building tests:
monkeypatch _send to capture the HTML, no network.
"""
import email_sender as es


def _capture(monkeypatch):
    box = {}

    def fake_send(to, subject, html, headers=None, reply_to=None, attachments=None):
        box.update(to=to, subject=subject, html=html, reply_to=reply_to,
                   attachments=attachments)
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


def test_signature_footer_matches_kyles_block(monkeypatch):
    """Kyle's real signature: one line of TREADWELL l phone l address, then the
    services line. Colours are set inline (navy brand word, grey details) so
    dark-mode clients recolour it as little as possible."""
    box = _capture(monkeypatch)
    es.send_portal_link("c@x.com", "Jane", "http://u", "Westport")
    html = box["html"]
    # Pin the navy TREADWELL span, not the bare word: the letterhead logo's alt text
    # also reads "Treadwell" and sits earlier in the HTML, so a loose needle would
    # stop testing the signature the moment either one is re-cased.
    brand = f'<span style="color:{es._SIG_NAVY}">TREADWELL</span>'
    assert brand in html
    assert "913.396.6216" in html
    assert "1707 E. 123rd Ter, Olathe, KS 66061" in html
    assert "Epoxy Flooring + Polished Concrete + Gypsum Underlayments" in html
    # order: brand → phone → address → services line
    assert html.index(brand) < html.index("913.396.6216") < html.index("1707 E. 123rd Ter")
    assert html.index("1707 E. 123rd Ter") < html.index("Epoxy Flooring +")
    assert "#000087" in html and "#595959" in html      # navy brand, grey detail
    assert "commercial epoxy &amp; polished concrete" not in html   # old footer retired
    # footer is on the reply + deposit emails too (single _wrap choke-point)
    box2 = _capture(monkeypatch)
    es.send_deposit_request("c@x.com", "http://u", "Westport", amount=100.0)
    assert "1707 E. 123rd Ter, Olathe, KS 66061" in box2["html"]


def test_every_email_carries_the_letterhead_logo(monkeypatch):
    """The Treadwell mark heads every email, and it has to survive real inboxes:
    an absolute public PNG (SVG never renders, a relative/localhost src is a broken
    image), sizing as ATTRIBUTES because Outlook ignores CSS sizing on images, and
    alt text because most clients block images by default."""
    monkeypatch.setattr(es.config, "PUBLIC_BASE_URL", "https://portal.wetreadwell.com")
    src = "https://portal.wetreadwell.com/static/img/treadwell-mark.png"

    senders = [
        lambda: es.send_otp("c@x.com", "123456", "Westport"),
        lambda: es.send_portal_link("c@x.com", "Jane", "http://u", "Westport"),
        lambda: es.send_reply_notification("c@x.com", "http://u", "Westport"),
        lambda: es.send_customer_update("c@x.com", "http://u", "Westport",
                                        "Deposit received", "<p>Thanks.</p>"),
        lambda: es.send_deposit_request("c@x.com", "http://u", "Westport", amount=100.0),
        lambda: es.notify_team("New reply", "<p>x</p>", recipients=["team@x.com"]),
    ]
    for send in senders:
        box = _capture(monkeypatch)
        send()
        html = box["html"]
        assert f'src="{src}"' in html                              # absolute, https, public
        assert 'alt="Treadwell"' in html
        assert 'width="150"' in html and 'height="90"' in html      # attributes, not just CSS
        assert "display:block" in html and "border:0" in html       # Outlook borders linked images
        assert ".svg" not in html                                   # SVG is refused by email clients
        # letterhead reads as letterhead: above the title and the signature
        assert html.index(src) < html.index("<h2")
        assert html.index(src) < html.index("913.396.6216")


def test_logo_url_follows_the_configured_base_url(monkeypatch):
    """Built at send time, so staging/local don't email a prod-only image URL."""
    monkeypatch.setattr(es.config, "PUBLIC_BASE_URL", "https://staging.portal.example")
    box = _capture(monkeypatch)
    es.send_portal_link("c@x.com", "Jane", "http://u", "Westport")
    assert 'src="https://staging.portal.example/static/img/treadwell-mark.png"' in box["html"]


def test_note_and_message_are_html_escaped(monkeypatch):
    box = _capture(monkeypatch)
    es.send_portal_link("c@x.com", "Jane", "http://u", "Westport",
                        note="<script>alert(1)</script>")
    assert "<script>" not in box["html"]
    assert "&lt;script&gt;" in box["html"]
