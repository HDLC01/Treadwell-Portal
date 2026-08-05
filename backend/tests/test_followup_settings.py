"""The follow-up cadence and its wording, as editable settings.

Hanz asked for the cadence to be editable — the intervals AND the four customer emails — by any
signed-in user, as one global setting. Until now both were Python constants: changing "every 3
days" to "every 5" meant a code edit and a deploy.

Everything these settings touch ends up in a CUSTOMER's inbox, which sets the bar. A wrong value
here is not an internal inconvenience like a mislabelled project; it is visible outside the
company and it repeats every few days until somebody notices. So the tests below are mostly about
the ways a well-meaning edit could go wrong:

  * **An interval of 1 hour** would chase somebody 24 times a day. Values are CLAMPED rather than
    rejected — a form that refuses a number without explaining is how people give up and ask an
    engineer — so the test is that the clamp exists and that it bites.
  * **A send window that ends before it opens** (18 to 8) would silence every customer email and
    look like the cadence had simply stopped. It falls back to the shipped window instead.
  * **A template with the link deleted** is a wasted send: those emails exist to get somebody back
    to the proposal. This is the one case that hard-refuses, because silently re-appending a button
    somebody deliberately removed is worse than saying no.
  * **HTML pasted into a template** would reach a customer's mail client as a broken layout that
    nobody sees until they do. Stripped, not escaped — escaping shows them a literal `&lt;b&gt;`.
  * **A missing settings table.** The DDL lands after the code on every environment, so an absent
    row must mean "the cadence as shipped" rather than "no cadence at all".

The last one is why `cfg` is an OPTIONAL argument all the way down: the 24 existing tests in
test_followup_rules.py pass unchanged, which is the evidence that this feature cannot have altered
the cadence for anybody who has not edited it.
"""
from datetime import datetime, timedelta, timezone

import pytest

import db
import email_sender
import followup_rules as R
import followup_settings as fs
import followup_worker as W

NOW = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)      # 09:00 Central, inside 8-18
DEFAULTS_ = fs.DEFAULTS


# ── the defaults are the shipped cadence ──────────────────────────────
def test_the_defaults_are_exactly_the_constants_the_cadence_shipped_with():
    """If these drift, every environment that has never opened the editor silently gets a
    different cadence than it had — the one change this feature must not make."""
    d = fs.defaults()
    assert d["first_nudge_hours"] == R.FIRST_NUDGE.total_seconds() / 3600 == 24
    assert d["second_nudge_hours"] == R.SECOND_NUDGE.total_seconds() / 3600 == 72
    assert d["recurring_hours"] == R.RECURRING.total_seconds() / 3600 == 72
    assert d["staff_personal_hours"] == R.STAFF_PERSONAL.total_seconds() / 3600 == 48
    assert d["max_recurring"] == R.MAX_RECURRING == 20
    assert d["send_start_hour"] == R.SEND_START_HOUR == 8
    assert d["send_end_hour"] == R.SEND_END_HOUR == 18


def test_the_default_templates_are_the_four_the_worker_asks_for():
    """`Due.template` names one of these. A missing key would mean an email with no wording."""
    assert set(fs.DEFAULT_TEMPLATES) == set(fs.TEMPLATE_KEYS)
    assert set(fs.TEMPLATE_KEYS) == {"not_viewed", "next_steps", "second_nudge", "checkin"}
    for key, t in fs.DEFAULT_TEMPLATES.items():
        assert t["subject"] and t["title"] and t["body"] and t["cta"], key
        assert "{link}" in t["body"], key


# ── clamping, not rejecting ───────────────────────────────────────────
@pytest.mark.parametrize("field,given,expect", [
    ("recurring_hours", 1, 4),              # below the floor -> the floor
    ("recurring_hours", 999999, 24 * 90),   # ceiling: 90 days
    ("max_recurring", 5000, 60),
])
def test_absurd_numbers_are_pulled_into_range(field, given, expect):
    assert fs.validate({field: given})[field] == expect


@pytest.mark.parametrize("field", ["first_nudge_hours", "second_nudge_hours",
                                   "recurring_hours", "staff_personal_hours", "max_recurring"])
@pytest.mark.parametrize("given", [0, -5, -999])
def test_zero_and_negatives_fall_back_rather_than_clamping_to_the_floor(field, given):
    """Clamping these to the floor would turn a typo into the most aggressive cadence the system
    allows — type 0 in the recurring box and customers get an email every four hours. A nonsense
    value is not an attempt at a fast schedule."""
    assert fs.validate({field: given})[field] == DEFAULTS_[field]


def test_an_out_of_range_hour_takes_the_whole_window_back_to_the_default():
    """Clamping start=99 to 23 would leave 23-to-18, which ends before it opens — so the window
    check catches it and restores 8-18. The pair has to stay coherent, not each field
    individually."""
    got = fs.validate({"send_start_hour": 99})
    assert (got["send_start_hour"], got["send_end_hour"]) == (8, 18)
    got = fs.validate({"send_end_hour": 0})
    assert (got["send_start_hour"], got["send_end_hour"]) == (8, 18)


def test_midnight_is_a_legitimate_window_start():
    """0 means midnight for this one field, so it must not be treated as "unset" like a 0 interval
    is. Staff mail is unclamped anyway, but a customer window of 0-8 is a real choice."""
    got = fs.validate({"send_start_hour": 0, "send_end_hour": 8})
    assert (got["send_start_hour"], got["send_end_hour"]) == (0, 8)


@pytest.mark.parametrize("junk", ["", None, "abc", True, False, {}, []])
def test_unreadable_numbers_fall_back_to_the_default(junk):
    assert fs.validate({"recurring_hours": junk})["recurring_hours"] == 72


def test_a_number_typed_as_text_still_works():
    """People type into a form; "48" arrives as a string."""
    assert fs.validate({"first_nudge_hours": "48"})["first_nudge_hours"] == 48
    assert fs.validate({"first_nudge_hours": " 48 "})["first_nudge_hours"] == 48


def test_a_window_that_ends_before_it_opens_falls_back():
    """THE dangerous one. 18-to-8 would silence every customer email and read as the cadence
    having stopped for no reason."""
    got = fs.validate({"send_start_hour": 18, "send_end_hour": 8})
    assert (got["send_start_hour"], got["send_end_hour"]) == (8, 18)


def test_an_equal_window_falls_back_too():
    got = fs.validate({"send_start_hour": 12, "send_end_hour": 12})
    assert (got["send_start_hour"], got["send_end_hour"]) == (8, 18)


def test_a_legitimate_narrow_window_is_kept():
    got = fs.validate({"send_start_hour": 10, "send_end_hour": 16})
    assert (got["send_start_hour"], got["send_end_hour"]) == (10, 16)


# ── templates ─────────────────────────────────────────────────────────
def test_a_template_without_the_link_is_refused():
    """These emails exist to get somebody back to the proposal. Without the button there is
    nothing to click, and silently re-appending one somebody deleted would be worse than saying
    no — so this is the single hard refusal."""
    with pytest.raises(fs.ValidationError) as e:
        fs.validate({"templates": {"checkin": {"body": "Hi {first_name}, any thoughts?"}}})
    assert "{link}" in str(e.value)


@pytest.mark.parametrize("key,label", list(fs.LABELS.items()))
def test_a_refusal_names_the_email_the_way_the_screen_does(key, label):
    """Read on staging: "The not viewed email needs {link}". That is a database key with the
    underscore taken out, and there is no tab called that — the tabs read "Not opened yet",
    "Recurring check-in". Somebody sent to fix the wrong one of four emails is worse off than
    somebody sent to fix none of them."""
    with pytest.raises(fs.ValidationError) as e:
        fs.validate({"templates": {key: {"body": "No button here at all."}}})
    msg = str(e.value)
    assert label in msg, "the refusal does not name the tab the person is looking at"
    assert key.replace("_", " ") not in msg or key == label, (
        "the refusal still leaks the raw database key")


def test_an_over_long_body_is_refused_by_its_screen_name_too():
    """The other refusal a person has to act on. Same reasoning."""
    with pytest.raises(fs.ValidationError) as e:
        fs.validate({"templates": {"second_nudge": {"body": "x" * 4100 + " {link}"}}})
    assert "Second reminder" in str(e.value)
    assert "4000" in str(e.value), "the limit is not stated, so there is nothing to trim towards"


def test_every_email_has_a_screen_name():
    """A missing label would silently fall back to the raw key — the bug this fixes."""
    assert set(fs.LABELS) == set(fs.TEMPLATE_KEYS)
    assert all(fs.label(k) == fs.LABELS[k] for k in fs.TEMPLATE_KEYS)
    assert "_" not in "".join(fs.LABELS.values())


def test_an_unknown_placeholder_is_refused_with_the_list_of_real_ones():
    """A typo like {name} would reach a customer verbatim. The message names the alternatives so
    nobody has to guess."""
    with pytest.raises(fs.ValidationError) as e:
        fs.validate({"templates": {"checkin": {"body": "Hi {name} {link}"}}})
    msg = str(e.value)
    assert "{name}" in msg and "{first_name}" in msg


def test_html_is_stripped_rather_than_escaped():
    """Escaping would show a customer a literal &lt;b&gt;, which looks broken. Stripping quietly
    gives them the sentence somebody meant to write."""
    got = fs.validate({"templates": {"checkin": {
        "title": "<b>Important</b>", "body": "<script>x</script>Hi {link}"}}})["templates"]["checkin"]
    assert got["title"] == "Important"
    assert "<" not in got["body"] and "script" not in got["body"]


def test_a_blank_field_keeps_the_shipped_wording():
    """Clearing a subject should not send an email with no subject."""
    got = fs.validate({"templates": {"checkin": {"subject": "", "body": "Hi {link}"}}})
    assert got["templates"]["checkin"]["subject"] == fs.DEFAULT_TEMPLATES["checkin"]["subject"]


def test_an_over_long_body_is_refused_rather_than_cut():
    """Truncating emails the customer half a sentence — and because {link} usually sits at the end,
    cutting the text silently removed the button and then the save was rejected for a missing link
    the author never deleted. Refusing says what is actually wrong."""
    with pytest.raises(fs.ValidationError) as e:
        fs.validate({"templates": {"checkin": {"body": "x" * 9000 + " {link}"}}})
    msg = str(e.value)
    assert "characters" in msg and "4000" in msg
    assert "{link}" not in msg, "the message blames the link when the real problem is length"


def test_a_body_at_the_limit_is_accepted():
    body = "x" * 3980 + " {link}"
    got = fs.validate({"templates": {"checkin": {"body": body}}})
    assert got["templates"]["checkin"]["body"].endswith("{link}")


def test_editing_one_template_leaves_the_other_three_alone():
    got = fs.validate({"templates": {"checkin": {"body": "Just this one {link}"}}})["templates"]
    assert got["checkin"]["body"] == "Just this one {link}"
    for key in ("not_viewed", "next_steps", "second_nudge"):
        assert got[key] == fs.DEFAULT_TEMPLATES[key], key


def test_an_unknown_template_name_is_refused():
    with pytest.raises(fs.ValidationError):
        fs.validate_template("nope", {"body": "{link}"})


# ── merge: a stored row over the defaults ─────────────────────────────
@pytest.mark.parametrize("stored", [None, {}, "nonsense", 5, [], {"templates": "nope"}])
def test_a_missing_or_corrupt_row_yields_the_shipped_cadence(stored):
    """The DDL lands after the code on every environment, so this is the normal state on the day
    of a deploy — not an edge case."""
    got = fs.merge(stored)
    assert got["first_nudge_hours"] == 24
    assert set(got["templates"]) == set(fs.TEMPLATE_KEYS)


def test_a_partial_row_keeps_the_defaults_for_everything_it_omits():
    got = fs.merge({"recurring_hours": 120})
    assert got["recurring_hours"] == 120
    assert got["first_nudge_hours"] == 24           # untouched
    assert got["send_end_hour"] == 18


def test_a_stored_template_that_has_gone_bad_falls_back_to_just_that_one():
    """A hand-edited row, or an older shape, must not take the whole cadence down — and must not
    take the other three emails with it."""
    got = fs.merge({"templates": {
        "checkin": {"body": "no link here"},                 # invalid
        "not_viewed": {"body": "Fine {link}", "subject": "Kept"},
    }})
    assert got["templates"]["checkin"] == fs.DEFAULT_TEMPLATES["checkin"]
    assert got["templates"]["not_viewed"]["subject"] == "Kept"


def test_stored_numbers_are_clamped_on_the_way_out_too():
    """Validation runs on save, but a row written by hand or by an older version reaches the
    worker without ever passing through it."""
    assert fs.merge({"recurring_hours": 1})["recurring_hours"] == 4


# ── the rules honour it ───────────────────────────────────────────────
def _sent(hours_ago, **extra):
    p = {"followup_enrolled_at": NOW - timedelta(hours=hours_ago),
         "proposal_status": "sent", "cycle_viewed_at": None}
    p.update(extra)
    return p


def test_no_config_behaves_exactly_as_the_constants_did():
    """The 24 tests in test_followup_rules.py assert this too, by passing nothing. Stated here as
    well because it is the promise the whole feature rests on."""
    assert [d.template for d in R.due_now(_sent(30), NOW)] == ["not_viewed", "staff_not_viewed"]
    assert R.due_now(_sent(20), NOW) == []


def test_a_longer_first_nudge_delays_the_email():
    """30 hours in: due by default, not due at 48."""
    assert R.due_now(_sent(30), NOW, {"first_nudge_hours": 48}) == []
    assert [d.template for d in R.due_now(_sent(50), NOW, {"first_nudge_hours": 48})] \
        == ["not_viewed", "staff_not_viewed"]


def test_a_shorter_first_nudge_brings_it_forward():
    assert [d.template for d in R.due_now(_sent(8), NOW, {"first_nudge_hours": 6})] \
        == ["not_viewed", "staff_not_viewed"]


def test_the_recurring_interval_is_honoured():
    """Unviewed and long past the first nudge: how many recurrences have matured depends on the
    interval, and the rule key carries the number — so a wrong interval would also break the
    dedupe that stops a customer being emailed twice."""
    p = _sent(24 + 72 * 3 + 1)
    slow = [d.rule_key for d in R.due_now(p, NOW, {"recurring_hours": 72})]
    fast = [d.rule_key for d in R.due_now(p, NOW, {"recurring_hours": 24})]
    assert any("nvr3" in k for k in slow), slow
    assert any("nvr9" in k or "nvr10" in k for k in fast), fast


def test_the_recurring_cap_is_honoured():
    p = _sent(24 + 72 * 40)
    capped = R.next_due_at(p, NOW, {"max_recurring": 2})
    assert capped is None, "a cap of 2 should have been exhausted long ago"
    assert R.next_due_at(p, NOW, {"max_recurring": 200}) is not None


def test_the_staff_nudge_interval_is_honoured():
    viewed = {"followup_enrolled_at": NOW - timedelta(hours=200),
              "proposal_status": "viewed",
              "cycle_viewed_at": NOW - timedelta(hours=30)}
    assert any(d.template == "staff_personal_followup"
               for d in R.due_now(viewed, NOW, {"staff_personal_hours": 24}))
    assert not any(d.template == "staff_personal_followup"
                   for d in R.due_now(viewed, NOW, {"staff_personal_hours": 48}))


def test_the_send_window_is_honoured():
    """09:00 Central. A window starting at 10 should hold the customer email back — and keep the
    staff one, which is unclamped because it lands in a work inbox."""
    got = R.due_now(_sent(30), NOW, {"send_start_hour": 10, "send_end_hour": 18})
    assert [d.audience for d in got] == ["staff"]


def test_a_corrupt_window_in_the_stored_config_does_not_silence_everything():
    """Validation prevents this on save, but a hand-edited row reaches the rules directly. Second
    line of defence, because the failure mode is silent."""
    assert R.in_send_window(NOW, {"send_start_hour": 18, "send_end_hour": 8}) is True


@pytest.mark.parametrize("bad", [{"first_nudge_hours": "abc"}, {"first_nudge_hours": None},
                                {"first_nudge_hours": -1}, {"first_nudge_hours": True},
                                {"recurring_hours": 0}, "nonsense", None, 5])
def test_junk_config_is_ignored_rather_than_crashing_the_tick(bad):
    """One bad settings row must not stop the cadence for every proposal."""
    got = R.due_now(_sent(30), NOW, bad if isinstance(bad, dict) else None)
    assert [d.template for d in got] == ["not_viewed", "staff_not_viewed"]


# ── the worker ────────────────────────────────────────────────────────
def test_the_worker_falls_back_when_the_settings_table_is_missing(monkeypatch):
    """This is the state on the day the code deploys and the DDL has not been applied. It must
    send exactly as it did before, not stop."""
    def boom(_key):
        raise RuntimeError('relation "portal_settings" does not exist')
    monkeypatch.setattr(W.db, "get_settings", boom)
    got = W._settings()
    assert got["first_nudge_hours"] == 24
    assert set(got["templates"]) == set(fs.TEMPLATE_KEYS)


def test_the_worker_uses_a_saved_cadence(monkeypatch):
    monkeypatch.setattr(W.db, "get_settings", lambda _k: {"recurring_hours": 120})
    assert W._settings()["recurring_hours"] == 120


def test_the_worker_reads_the_settings_once_per_tick_not_once_per_proposal(monkeypatch):
    """An edit landing mid-tick would otherwise apply to some candidates and not others — an
    inconsistency nobody could reproduce afterwards.

    The proposals below are deliberately LIVE and freshly enrolled: status "sent" so the loop does
    not skip them, and enrolled just now so nothing is due and no email is attempted. An earlier
    version of this test used approved proposals, which the loop skips before it ever reaches the
    settings — so a per-proposal read went unnoticed. Verified by making the worker read per row
    and watching this fail."""
    reads = []
    monkeypatch.setattr(W.db, "get_settings", lambda k: reads.append(k) or {})
    monkeypatch.setattr(W, "_enabled", lambda: True)
    monkeypatch.setattr(W.db, "list_followup_candidates",
                        lambda: [{"proposal_id": "a"}, {"proposal_id": "b"},
                                 {"proposal_id": "c"}])
    monkeypatch.setattr(W.db, "get_proposal", lambda pid: {
        "proposal_id": pid, "proposal_status": "sent",
        "followup_enrolled_at": NOW, "cycle_viewed_at": None})
    W._tick(NOW)
    assert len(reads) == 1, "settings were read %d times for 3 candidates" % len(reads)


# ── the emails ────────────────────────────────────────────────────────
@pytest.fixture()
def captured(monkeypatch):
    box = {}
    monkeypatch.setattr(email_sender, "_send",
                        lambda to, subj, html, hdrs=None, reply_to=None:
                        box.update(to=to, subject=subj, html=html) or True)
    return box


def test_with_no_saved_wording_the_shipped_email_goes_out(captured):
    email_sender.send_followup("a@b.com", "https://x/p/tok", "Westport", "next_steps", name="Dave")
    assert captured["subject"] == "Next steps for Westport"
    assert "Review and approve" in captured["html"]


def test_saved_wording_replaces_it(captured):
    tpl = {"next_steps": {"subject": "Following up on {project}", "title": "A nudge",
                          "body": "Hi {first_name},\n\nWe need {need}.\n\n{link}\n\nThanks!",
                          "cta": "Open the proposal"}}
    email_sender.send_followup("a@b.com", "https://x/p/tok", "Westport", "next_steps",
                               name="Dave Smith", templates=tpl)
    h = captured["html"]
    assert captured["subject"] == "Following up on Westport"
    assert "Hi Dave," in h, "first name not substituted"
    assert "signed approval and the deposit" in h, "{need} not substituted"
    assert "Open the proposal" in h, "custom button label not used"
    assert "{link}" not in h and "{project}" not in h, "a raw token reached the email"


def test_the_deposit_phrase_still_follows_the_job(captured):
    """{need} exists because promising a deposit on a job sent without one would be wrong, and GC
    work usually is. An edited template must not lose that."""
    tpl = {"checkin": {"subject": "s", "title": "t", "body": "We need {need}. {link}", "cta": "c"}}
    email_sender.send_followup("a@b.com", "u", "P", "checkin", templates=tpl,
                               deposit_required=False)
    assert "signed approval" in captured["html"]
    assert "deposit" not in captured["html"].split("signed approval")[1][:40]


def test_plain_text_becomes_paragraphs(captured):
    tpl = {"checkin": {"subject": "s", "title": "t",
                       "body": "One.\n\nTwo.\n\n{link}\n\nThree.", "cta": "c"}}
    email_sender.send_followup("a@b.com", "u", "P", "checkin", templates=tpl)
    assert captured["html"].count("<p>") >= 3


def test_a_partial_saved_template_falls_back_for_that_email_only(captured):
    """Only three of four edited, or one cleared: the rest must still send properly."""
    email_sender.send_followup("a@b.com", "u", "Westport", "checkin",
                               templates={"next_steps": {"body": "{link}"}})
    assert captured["subject"] == "Checking in on Westport", "should have used the shipped wording"


def test_the_status_ask_survives_saved_wording(captured):
    """The "delayed / not moving forward" escape hatch is what stops the recurring series being a
    dead end for the customer. An edited template must not drop it."""
    tpl = {"checkin": {"subject": "s", "title": "t", "body": "{link}", "cta": "c"}}
    email_sender.send_followup("a@b.com", "u", "P", "checkin", templates=tpl,
                               token="tok", include_status_ask=True)
    assert "tok" in captured["html"]


# ── the preview ───────────────────────────────────────────────────────
def test_the_preview_fills_every_token():
    """The editor's whole safety net: an unfilled token or a missing button is obvious here and
    invisible in the form."""
    p = fs.preview(fs.defaults(), "next_steps")
    for token in fs.TOKENS:
        assert token not in p["subject"] and token not in p["body"], token
    assert "Dave" in p["body"] and "Westport" in p["body"]


def test_the_preview_shows_the_button_label():
    p = fs.preview(fs.defaults(), "not_viewed")
    assert "View your proposal" in p["body"]


def test_the_preview_of_an_unknown_template_does_not_crash():
    assert isinstance(fs.preview(fs.defaults(), "nope"), dict)


# ── the endpoints ─────────────────────────────────────────────────────
from fastapi.testclient import TestClient           # noqa: E402

import main as portal_main                          # noqa: E402

client = TestClient(portal_main.app)
HDRS = {"X-Service-Token": "test-token"}


@pytest.fixture(autouse=True)
def _service_token(monkeypatch):
    monkeypatch.setattr(portal_main, "_admin_ok", lambda request: True)


def test_get_returns_a_usable_cadence_even_with_no_row(monkeypatch):
    """The state on the day the code deploys and the DDL has not been applied."""
    monkeypatch.setattr(portal_main.db, "get_settings", lambda k: None)
    monkeypatch.setattr(portal_main.db, "settings_meta", lambda k: {})
    j = client.get("/api/admin/settings/followups").json()
    assert j["ok"] is True
    assert j["settings"]["first_nudge_hours"] == 24
    assert j["saved"] is False, "a fresh install must not look like somebody's choice"
    assert set(j["previews"]) == set(fs.TEMPLATE_KEYS)


def test_get_survives_the_table_not_existing(monkeypatch):
    def boom(_k):
        raise RuntimeError('relation "portal_settings" does not exist')
    monkeypatch.setattr(portal_main.db, "get_settings", boom)
    monkeypatch.setattr(portal_main.db, "settings_meta", boom)
    j = client.get("/api/admin/settings/followups").json()
    assert j["ok"] is True and j["settings"]["recurring_hours"] == 72


def test_get_reports_who_last_changed_it(monkeypatch):
    """These settings send email to customers, so "who and when" has to be answerable."""
    monkeypatch.setattr(portal_main.db, "get_settings", lambda k: {"recurring_hours": 120})
    monkeypatch.setattr(portal_main.db, "settings_meta",
                        lambda k: {"updated_at": NOW, "updated_by": "hanz@wetreadwell.com"})
    j = client.get("/api/admin/settings/followups").json()
    assert j["saved"] is True
    assert j["updated_by"] == "hanz@wetreadwell.com"
    assert j["settings"]["recurring_hours"] == 120


def test_put_saves_and_returns_what_was_actually_stored(monkeypatch):
    """Returning the stored values matters because numbers are clamped: somebody who typed 2 hours
    needs to see they got 4, not believe their edit took."""
    saved = {}
    monkeypatch.setattr(portal_main.db, "save_settings",
                        lambda k, v, by=None: saved.update(key=k, value=v, by=by))
    r = client.put("/api/admin/settings/followups",
                   json={"settings": {"recurring_hours": 2}, "by": "hanz@wetreadwell.com"})
    j = r.json()
    assert r.status_code == 200 and j["ok"] is True
    assert j["settings"]["recurring_hours"] == 4, "the clamp is not reflected back"
    assert saved["value"]["recurring_hours"] == 4, "the unclamped value was stored"
    assert saved["by"] == "hanz@wetreadwell.com"


def test_put_returns_the_audit_line_so_the_editor_need_not_reload(monkeypatch):
    """Found on staging. The save stored the edit and the page went on saying "Never changed —
    this is the cadence as shipped" until somebody reloaded, because only the GET carried these
    fields. The line exists to answer one question and was answering it wrongly at the one moment
    anybody had reason to read it."""
    monkeypatch.setattr(portal_main.db, "save_settings",
                        lambda k, v, by=None: {"updated_at": NOW, "updated_by": by})
    j = client.put("/api/admin/settings/followups",
                   json={"settings": {}, "by": "kyle@wetreadwell.com"}).json()
    assert j["saved"] is True
    assert j["updated_by"] == "kyle@wetreadwell.com"
    assert j["updated_at"], "no timestamp, so the editor cannot say when"


def test_the_audit_line_comes_back_from_the_write_not_a_second_query(monkeypatch):
    """One round trip. A follow-up select would be another chance to fail or hang on a path that
    has already succeeded, and would read a timestamp taken slightly after the one stored."""
    monkeypatch.setattr(portal_main.db, "save_settings",
                        lambda k, v, by=None: {"updated_at": NOW, "updated_by": by})
    monkeypatch.setattr(portal_main.db, "settings_meta",
                        lambda k: pytest.fail("the save path re-read the audit row"))
    r = client.put("/api/admin/settings/followups",
                   json={"settings": {}, "by": "will@wetreadwell.com"})
    assert r.status_code == 200 and r.json()["updated_by"] == "will@wetreadwell.com"


def test_a_save_whose_audit_values_come_back_empty_still_reports_the_save(monkeypatch):
    """The write succeeded. Going quiet about it, or failing the request, would tell somebody
    their edit was lost when it was not."""
    monkeypatch.setattr(portal_main.db, "save_settings", lambda k, v, by=None: {})
    r = client.put("/api/admin/settings/followups",
                   json={"settings": {}, "by": "will@wetreadwell.com"})
    j = r.json()
    assert r.status_code == 200 and j["ok"] is True and j["saved"] is True
    assert j["updated_by"] == "will@wetreadwell.com", (
        "with no audit values returned, fall back to who the request said it was")


def test_the_upsert_asks_for_its_own_audit_values_back(monkeypatch):
    """Every other test here replaces save_settings, so nothing exercised the statement itself —
    a mutation removing the `returning` clause passed the whole suite. The endpoint's contract is
    that the write hands back what it wrote; this is the only test that holds the write to it."""
    seen = {}

    def fake_q1(sql, params=()):
        seen["sql"] = " ".join(sql.split())
        seen["params"] = params
        return {"updated_at": NOW, "updated_by": "kyle@wetreadwell.com"}

    monkeypatch.setattr(portal_main.db, "q1", fake_q1)
    got = portal_main.db.save_settings("followups", {"recurring_hours": 72}, "kyle@wetreadwell.com")

    assert "returning updated_at, updated_by" in seen["sql"].lower(), (
        "the write does not ask for the audit values, so the editor cannot show them")
    assert "on conflict" in seen["sql"].lower(), "an upsert that would fail the second time"
    assert got["updated_by"] == "kyle@wetreadwell.com", "the returned row is dropped on the floor"


def test_a_write_that_returns_nothing_gives_back_a_dict_not_none(monkeypatch):
    """The endpoint does `or {}`, but a None here would also break any future caller that reads
    the result directly."""
    monkeypatch.setattr(portal_main.db, "q1", lambda sql, params=(): None)
    assert portal_main.db.save_settings("followups", {}, None) == {}


def test_get_tells_the_editor_what_each_email_is_called(monkeypatch):
    """So the tab labels and the refusal messages come from one place and cannot disagree."""
    monkeypatch.setattr(portal_main.db, "get_settings", lambda k: None)
    monkeypatch.setattr(portal_main.db, "settings_meta", lambda k: {})
    j = client.get("/api/admin/settings/followups").json()
    assert j["labels"] == dict(fs.LABELS)
    assert set(j["labels"]) == set(j["previews"]), "a preview with no name, or a name with none"


def test_put_refuses_a_template_that_will_not_send(monkeypatch):
    monkeypatch.setattr(portal_main.db, "save_settings", lambda k, v, by=None: None)
    r = client.put("/api/admin/settings/followups",
                   json={"settings": {"templates": {"checkin": {"body": "no button"}}}})
    assert r.status_code == 400
    assert "{link}" in r.json()["error"]


def test_put_reports_a_storage_failure_rather_than_claiming_success(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('relation "portal_settings" does not exist')
    monkeypatch.setattr(portal_main.db, "save_settings", boom)
    r = client.put("/api/admin/settings/followups", json={"settings": {}})
    assert r.status_code == 500
    assert "settings table" in r.json()["error"]


def test_an_empty_payload_is_how_a_reset_is_expressed(monkeypatch):
    """The editor sends {} to reset, so the server is the only place that defines "default" —
    no second definition in the browser to drift from this one."""
    saved = {}
    monkeypatch.setattr(portal_main.db, "save_settings",
                        lambda k, v, by=None: saved.update(value=v))
    j = client.put("/api/admin/settings/followups", json={"settings": {}}).json()
    assert j["settings"]["first_nudge_hours"] == 24
    assert saved["value"]["templates"]["checkin"] == fs.DEFAULT_TEMPLATES["checkin"]


def test_preview_renders_without_saving(monkeypatch):
    called = []
    monkeypatch.setattr(portal_main.db, "save_settings",
                        lambda *a, **k: called.append(1))
    r = client.post("/api/admin/settings/followups/preview", json={"settings": {"templates": {
        "checkin": {"subject": "Hi {project}", "title": "t", "body": "Hello {first_name} {link}",
                    "cta": "Open"}}}})
    j = r.json()
    assert r.status_code == 200
    assert j["previews"]["checkin"]["subject"] == "Hi Westport Retail Center"
    assert "Dave" in j["previews"]["checkin"]["body"]
    assert not called, "a preview must never write"


def test_preview_reports_why_a_template_will_not_send():
    r = client.post("/api/admin/settings/followups/preview",
                    json={"settings": {"templates": {"checkin": {"body": "no button"}}}})
    assert r.status_code == 400
    assert "{link}" in r.json()["error"]


def test_the_endpoints_require_the_service_token(monkeypatch):
    monkeypatch.setattr(portal_main, "_admin_ok", lambda request: False)
    assert client.get("/api/admin/settings/followups").status_code == 401
    assert client.put("/api/admin/settings/followups", json={}).status_code == 401
    assert client.post("/api/admin/settings/followups/preview", json={}).status_code == 401


# ── a read that FAILED is not a read that came back empty ──────────────────────
# The worst finding of the audit over this batch. The GET collapsed every failure of get_settings
# to stored=None, returned 200 with saved:false, and the editor then showed the shipped defaults
# under the words "Never changed". One press of Save replaced the real row - five intervals, the
# send window and the hand-written wording of all four customer emails - with boilerplate, on a
# single-row table with no history, attributed to whoever pressed the button.
class _Undefined(Exception):
    sqlstate = "42P01"                    # undefined_table


def test_a_missing_table_still_means_as_shipped(monkeypatch):
    """The one failure that really does mean "nothing is configured": prod cannot apply its own
    DDL, so code arrives before the table and the cadence must keep working."""
    monkeypatch.setattr(db, "q1", lambda *a, **k: (_ for _ in ()).throw(_Undefined("no relation")))
    assert db.get_settings("followups") is None


def test_any_other_read_failure_is_reported_not_swallowed(monkeypatch):
    """Because the caller will offer to OVERWRITE the row it could not read."""
    monkeypatch.setattr(db, "q1",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("connection reset")))
    with pytest.raises(db.SettingsUnreadable):
        db.get_settings("followups")


def test_missing_table_is_told_apart_from_a_real_failure():
    assert db._is_missing_table(_Undefined("relation does not exist")) is True
    assert db._is_missing_table(RuntimeError('relation "portal_settings" does not exist')) is True
    assert db._is_missing_table(RuntimeError("connection reset by peer")) is False
    assert db._is_missing_table(TimeoutError()) is False


def test_a_wrapped_missing_table_is_still_recognised():
    """The pool can re-raise, so the sqlstate may be one level down."""
    outer = RuntimeError("query failed")
    outer.__cause__ = _Undefined("nope")
    assert db._is_missing_table(outer) is True


def test_get_reports_an_unreadable_row_instead_of_calling_it_never_configured(monkeypatch):
    def boom(_k):
        raise db.SettingsUnreadable("connection reset")
    monkeypatch.setattr(portal_main.db, "get_settings", boom)
    monkeypatch.setattr(portal_main.db, "settings_meta", lambda k: {})
    j = client.get("/api/admin/settings/followups").json()
    assert j["ok"] is True, "the page must still render, with the defaults, to say what happened"
    assert j["read_failed"] is True, (
        "a failed read looks identical to an empty one, so the editor will offer to overwrite it")
    assert j["saved"] is False


def test_a_missing_table_is_not_reported_as_a_failed_read(monkeypatch):
    """Otherwise every environment awaiting its DDL shows a scary warning and a dead Save."""
    monkeypatch.setattr(portal_main.db, "get_settings", lambda k: None)
    monkeypatch.setattr(portal_main.db, "settings_meta", lambda k: {})
    j = client.get("/api/admin/settings/followups").json()
    assert j["read_failed"] is False and j["saved"] is False


def test_a_blip_on_the_decorative_caption_does_not_discard_the_config(monkeypatch):
    """settings_meta only feeds "who last changed this". It used to share the try with the real
    read, so a failure on the caption threw away a config that had been read perfectly well."""
    monkeypatch.setattr(portal_main.db, "get_settings", lambda k: {"recurring_hours": 120})
    monkeypatch.setattr(portal_main.db, "settings_meta",
                        lambda k: (_ for _ in ()).throw(RuntimeError("statement timeout")))
    j = client.get("/api/admin/settings/followups").json()
    assert j["settings"]["recurring_hours"] == 120, "a saved cadence was thrown away"
    assert j["saved"] is True
    assert j["read_failed"] is False


def test_the_worker_keeps_sending_when_the_settings_read_fails(monkeypatch):
    """Falling back to the shipped cadence is right for the WORKER - stopping the chase would be
    worse than chasing on the old schedule. Only the editor must refuse to act."""
    monkeypatch.setattr(W.db, "get_settings",
                        lambda k: (_ for _ in ()).throw(db.SettingsUnreadable("reset")))
    cfg = W._settings()
    assert cfg["recurring_hours"] == fs.DEFAULTS["recurring_hours"]


def test_the_pipeline_reads_the_new_columns_so_a_pre_DDL_prod_survives():
    """The audit's worst finding, and nothing tested this query.

    `list_all_portal_proposals` is the ONLY pipeline query. Naming a column prod does not have yet
    makes psycopg raise UndefinedColumn, which 500s /api/admin/pipeline, which the tool turns into
    a 502 on both the CRM board and the Follow-ups page — with an error that blames portal
    reachability, so somebody goes looking at nginx over one missing ALTER. Prod cannot apply its
    own DDL, so the code genuinely does arrive first.

    A jsonb key lookup returns NULL for an absent key instead, and starts working the moment the
    column lands. Verified against the prod database before relying on it."""
    seen = {}
    import db as real_db
    orig = real_db.qall
    try:
        real_db.qall = lambda sql, params=(): seen.setdefault("sql", " ".join(sql.split())) and []
        real_db.list_all_portal_proposals()
    finally:
        real_db.qall = orig
    sql = seen["sql"]
    for col in ("link_clicked_at", "last_link_clicked_at"):
        assert "p.%s" % col not in sql, (
            "%s is named directly; a prod that has not had the ALTER applied will 500 the whole "
            "staff board on this one query" % col)
        assert "to_jsonb(p) ->> '%s'" % col in sql, "%s is not read at all any more" % col
    # The columns that have always existed stay direct — the indirection is only for the new ones,
    # and turning the whole select into jsonb lookups would hide a genuine typo.
    assert "p.proposal_status" in sql and "p.viewed_at" in sql
