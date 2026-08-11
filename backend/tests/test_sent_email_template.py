"""The first "your proposal is ready" email is editable, and the revised one deliberately is not.

Hanz, 2026-08-12:

    "Then in this Cadence and email section as well. Create the ability to change what the first
     proposal sent email looks like. from the heading to the content (this would be the global
     setting for the first proposal sent) Just like the emails for the follow ups."

WHY "sent" IS NOT IN TEMPLATE_KEYS. That tuple is what followup_rules and followup_worker walk to
decide what to CHASE with. Adding the sent email to it would make the cadence send
"your proposal is ready" as a reminder three days later. So the editable set is a superset
(ALL_TEMPLATE_KEYS) and the cadence set is untouched — every existing loop over the four stays a
loop over the four, and this file asserts that separation directly, because it is the one mistake
here that would reach a customer.

WHY THE REVISED EMAIL KEEPS ITS OWN WORDING. Asked whether the template should drive re-sends too,
Hanz chose first-send-only. That wording ("It replaces the version we sent previously") is the only
thing telling a customer that the numbers they already have no longer stand, and that the portal
has reopened it for approval. An edited template silently replacing it is how somebody approves the
wrong figures.

The send goes through exactly the pipeline send_followup uses — same tokens, same
blank-line-separated blocks, same {link}-becomes-the-button rule — so the editor's own preview is
honest about what actually goes out.
"""
import inspect

import pytest

import config
import email_sender
import followup_settings

PROJECT = "Westport Retail Center"
TOKEN = "tokABC123"
CUSTOMER = "dana.reed@acme.com"

EDITED = {
    "title": "Here is your Treadwell bid",
    "body": "Hi {first_name},\n\nBid for {project} attached.\n\n{link}\n\nCheers.",
    "cta": "Open the bid",
}


@pytest.fixture
def sent(monkeypatch):
    out = []

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        out.append(json)

        class R:
            def raise_for_status(self):
                return None
        return R()

    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "EMAIL_REPLY_TO", "")
    monkeypatch.setattr(email_sender.httpx, "post", fake_post)
    # The subject is its own feature with its own tests; pin it so a change there cannot fail here.
    monkeypatch.setattr(email_sender, "_thread_subject_template",
                        lambda: "Your Treadwell proposal — {project}")
    return out


def _send(sent, monkeypatch, tpl, **kw):
    monkeypatch.setattr(email_sender, "_sent_template", lambda: tpl)
    email_sender.send_portal_link(CUSTOMER, "Dana Reed", "https://portal.example.com/p/" + TOKEN,
                                  PROJECT, token=TOKEN, **kw)
    return sent[-1]["html"]


# ── the separation that keeps the cadence honest ─────────────────────────────
def test_the_worker_cannot_chase_with_the_sent_email():
    """THE one that matters. followup_rules and followup_worker walk TEMPLATE_KEYS; if "sent"
    were in it, a customer would get "your proposal is ready" again three days later."""
    assert followup_settings.SENT_KEY not in followup_settings.TEMPLATE_KEYS
    assert followup_settings.SENT_KEY in followup_settings.ALL_TEMPLATE_KEYS
    # The sent email is the ONLY thing the editable set adds. Written as a difference rather than
    # a literal tuple: the cadence set grew a deposit reminder on 2026-08-12, and a test that
    # spells out today's four has to be edited every time the cadence gains a stage — which is how
    # a real separation check turns into a chore somebody deletes.
    assert set(followup_settings.ALL_TEMPLATE_KEYS) - set(followup_settings.TEMPLATE_KEYS) == {
        followup_settings.SENT_KEY}


def test_the_worker_still_only_knows_the_four():
    """Asserted against the worker's own source, not just the tuple: a loop rewritten to walk
    ALL_TEMPLATE_KEYS would pass the test above and still send the wrong email."""
    import followup_worker
    src = inspect.getsource(followup_worker)
    assert "ALL_TEMPLATE_KEYS" not in src, (
        "the follow-up worker walks the editable set, so the cadence can now chase with the "
        "proposal-sent email")
    import followup_rules
    assert "ALL_TEMPLATE_KEYS" not in inspect.getsource(followup_rules)


def test_it_is_editable_and_validated_like_the_others():
    got = followup_settings.validate({"templates": {"sent": EDITED}})
    assert got["templates"]["sent"]["title"] == "Here is your Treadwell bid"
    assert got["templates"]["sent"]["cta"] == "Open the bid"


def test_the_link_is_required_here_too():
    """Without it the email has nothing to click, which for the FIRST email means the customer
    never reaches the proposal at all."""
    with pytest.raises(followup_settings.ValidationError) as e:
        followup_settings.validate({"templates": {"sent": {"body": "no link here"}}})
    assert "{link}" in str(e.value)


def test_the_shipped_default_is_what_customers_have_been_getting():
    """The editor opens showing the real wording rather than a blank box — the same rule the four
    follow-up templates already follow."""
    d = followup_settings.defaults()["templates"]["sent"]
    assert d["title"] == "Your proposal is ready"
    assert "{project}" in d["body"] and "{link}" in d["body"]
    assert d["cta"] == "View your proposal"


def test_it_has_a_tab_name_and_a_when_it_fires_heading():
    assert followup_settings.LABELS["sent"] == "Proposal sent"
    assert "first email" in followup_settings.EDITOR_TITLES["sent"]


# ── what actually goes out ───────────────────────────────────────────────────
def test_an_edited_template_is_what_the_customer_receives(sent, monkeypatch):
    html = _send(sent, monkeypatch, EDITED)
    assert "Here is your Treadwell bid" in html, "the saved heading is ignored"
    assert "Hi Dana," in html, "{first_name} was not substituted"
    assert PROJECT in html, "{project} was not substituted"
    assert "Open the bid" in html, "the saved button label is ignored"
    assert "Cheers." in html, "a block after the link was dropped"
    assert "{link}" not in html and "{project}" not in html, "a raw token reached the customer"


def test_the_estimators_note_still_sits_above_the_button(sent, monkeypatch):
    """It has always been there, and it is the personal line the customer reads first. An edited
    template must not move it below the call to action."""
    html = _send(sent, monkeypatch, EDITED, note="Call me Friday if the dates are tight.")
    assert "Call me Friday" in html
    assert html.index("Call me Friday") < html.index("Open the bid")


def test_a_note_survives_a_template_whose_link_is_mid_sentence(sent, monkeypatch):
    """The note is inserted as its own block immediately before the link BLOCK. A template that
    writes {link} inside a sentence has no such block, and the note must not vanish."""
    html = _send(sent, monkeypatch,
                 {"title": "T", "body": "See the bid here: {link} — thanks.", "cta": "Open"},
                 note="NOTE HERE")
    assert "NOTE HERE" in html


def test_an_unreadable_template_falls_back_to_the_shipped_copy(sent, monkeypatch):
    """Publishing a proposal must never fail over a settings read. None means "use the hardcoded
    wording", which IS the shipped default and is the one thing guaranteed to render."""
    html = _send(sent, monkeypatch, None)
    assert "Your proposal for" in html and PROJECT in html
    assert "View your proposal" in html


def test_a_stored_template_with_no_body_falls_back_too(sent, monkeypatch):
    html = _send(sent, monkeypatch, {"title": "T", "body": "", "cta": "c"})
    assert "Your proposal for" in html, "an empty body produced an empty email"


def test_the_subject_is_still_the_project_thread(sent, monkeypatch):
    """Editing the email must not split the Gmail conversation the whole project shares."""
    _send(sent, monkeypatch, EDITED)
    assert sent[-1]["subject"] == "Your Treadwell proposal — " + PROJECT


def test_the_proposal_anchor_still_rides_along(sent, monkeypatch):
    """It is how a customer's reply finds its project. Rendering a different body must not drop
    the threading headers."""
    import inbound
    _send(sent, monkeypatch, EDITED)
    assert inbound.find_thread_token(sent[-1]["headers"]) == TOKEN


# ── the revised send is deliberately untouched ───────────────────────────────
def test_a_revised_send_keeps_the_replaces_previous_wording(sent, monkeypatch):
    """The only thing telling the customer the numbers they hold no longer stand."""
    html = _send(sent, monkeypatch, EDITED, revised=True)
    assert "replaces the" in html
    assert "Here is your Treadwell bid" not in html, (
        "an edited template overrode the revised-proposal email, so a customer can be told a "
        "re-send is the original")
    assert "View the revised proposal" in html


def test_a_revised_send_does_not_even_read_the_template(sent, monkeypatch):
    """Not merely ignored — not consulted. A settings read on a path that cannot use the result
    is a 30-second stall waiting to happen."""
    called = []
    monkeypatch.setattr(email_sender, "_sent_template",
                        lambda: called.append(1) or EDITED)
    email_sender.send_portal_link(CUSTOMER, "Dana", "https://p/x", PROJECT, token=TOKEN,
                                  revised=True)
    assert called == [], "the revised path read the sent template"


# ── cost ─────────────────────────────────────────────────────────────────────
@pytest.mark.realwording
def test_the_template_read_is_cached():
    """It runs once per RECIPIENT on every publish. Without a cache a connection-pool stall costs
    30 seconds per address; success-only, so one blip cannot pin the fallback for a minute."""
    src = inspect.getsource(email_sender._sent_template)
    assert "_sent_tpl_cache" in src and "time.monotonic()" in src
    # Scoped to the EXCEPT block, not "somewhere before the first return None". The loose
    # ordering check passed while a cache write sat inside the handler, because an earlier
    # `return None` (the body-less guard) came first — that mutation survived.
    i = src.index("except Exception")
    assert "return None" in src[i:], "a failed read does not fall back"
    # The failure path must RETURN before any cache write. Compared as positions after the
    # `except`, because slicing to the end of the function swept in the success write and the
    # mutation survived; slicing "somewhere before the first return None" hit the body-less guard
    # higher up and survived too.
    assert src.index("return None", i) < src.index("_sent_tpl_cache = (", i), (
        "a failed read is cached, so one connection blip pins the shipped copy for a minute")


def test_the_editor_can_preview_it():
    """A tab with no preview renders as a broken panel."""
    d = followup_settings.defaults()
    pv = followup_settings.preview(d, "sent")
    assert pv.get("title") and pv.get("body")
    for token in followup_settings.TOKENS:
        assert token not in pv["title"] and token not in pv["body"], token
