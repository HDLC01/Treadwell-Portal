"""The follow-up emails' WORDING as editable settings: templates, rendering, previews and the
settings endpoints' storage behaviour.

The SCHEDULE half (2026-10-28: 1st/2nd email per step with switches, days 1-90 refused rather than
clamped, the old row migrated on read) is tested in test_followup_auto_settings.py; this file's
clamping and old-cadence tests went with the old shape.

Everything here ends up in a CUSTOMER's inbox:

  * **A template with the link deleted** is a wasted send, so it is refused.
  * **HTML pasted into a template** is stripped, not escaped.
  * **A missing settings table** means "as shipped", never "no follow-ups".
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


# ── the send window ───────────────────────────────────────────────────
def test_midnight_is_a_legitimate_window_start():
    """0 means midnight for this one field, so it must not be treated as "unset" like a 0 interval
    is. Staff mail is unclamped anyway, but a customer window of 0-8 is a real choice."""
    got = fs.validate({"send_start_hour": 0, "send_end_hour": 8})
    assert (got["send_start_hour"], got["send_end_hour"]) == (0, 8)


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
    # A SUBSTRING check was a false positive the moment a label legitimately contained its own
    # key: "sent" is a word in "Proposal sent". What must not leak is the de-underscored key
    # standing on its own — "the not viewed email" — so compare against the message with the
    # label removed, which is the only place a leak could hide.
    leaked = msg.replace(label, "")
    assert key.replace("_", " ") not in leaked, (
        "the refusal still leaks the raw database key %r" % key)


def test_every_email_has_a_screen_name():
    """A missing label would silently fall back to the raw key — the bug this fixes."""
    # ALL_TEMPLATE_KEYS, not TEMPLATE_KEYS: the editor has a fifth tab (the proposal-sent
    # email), and a tab with no screen name would render as its raw database key. The
    # cadence set stays four — see test_the_default_templates_cover_every_editable_email.
    assert set(fs.LABELS) == set(fs.ALL_TEMPLATE_KEYS)
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
    """Clearing a field should not send an email missing it. The subject is now one per
    project rather than one per template, so that is where this is asserted."""
    got = fs.validate({"thread_subject": "", "templates": {"checkin": {"body": "Hi {link}"}}})
    assert got["thread_subject"] == fs.DEFAULT_THREAD_SUBJECT
    got = fs.validate({"templates": {"checkin": {"title": "", "body": "Hi {link}"}}})
    assert got["templates"]["checkin"]["title"] == fs.DEFAULT_TEMPLATES["checkin"]["title"]


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


def test_a_stored_template_that_has_gone_bad_falls_back_to_just_that_one():
    """A hand-edited row, or an older shape, must not take the whole cadence down — and must not
    take the other three emails with it."""
    got = fs.merge({"templates": {
        "checkin": {"body": "no link here"},                 # invalid
        "not_viewed": {"body": "Fine {link}", "title": "Kept"},
    }})
    assert got["templates"]["checkin"] == fs.DEFAULT_TEMPLATES["checkin"]
    assert got["templates"]["not_viewed"]["title"] == "Kept"


# ── the rules honour it ───────────────────────────────────────────────
def _sent(hours_ago, **extra):
    p = {"followup_enrolled_at": NOW - timedelta(hours=hours_ago),
         "proposal_status": "sent", "cycle_viewed_at": None}
    p.update(extra)
    return p


def test_two_schedules_that_genuinely_coincide_still_dedupe():
    """Not every collision is a bug. 24h and 72h share occurrences — every third one of the fast
    schedule IS one of the slow schedule — so when both mature at the same moment, one email is
    the right answer. The key is the occurrence time precisely so that this case, and only this
    case, collides."""
    p = _sent(24 + 72 * 3 + 1)
    a = [d.rule_key for d in R.due_now(p, NOW, {"recurring_hours": 72}) if "nvr" in d.rule_key]
    b = [d.rule_key for d in R.due_now(p, NOW, {"recurring_hours": 24}) if "nvr" in d.rule_key]
    assert a == b, (a, b)


def test_a_corrupt_window_in_the_stored_config_does_not_silence_everything():
    """Validation prevents this on save, but a hand-edited row reaches the rules directly. Second
    line of defence, because the failure mode is silent."""
    assert R.in_send_window(NOW, {"send_start_hour": 18, "send_end_hour": 8}) is True


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
    monkeypatch.setattr(W.db, "followup_signals", lambda pid: {})
    monkeypatch.setattr(W.db, "list_pending_followup_tasks", lambda: [])
    monkeypatch.setattr(W.db, "list_stale_sending_tasks", lambda minutes: [])
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
    # The SUBJECT belongs to the project thread now, shared with every other update about
    # this job. A template controls the heading, the body and the button.
    assert captured["subject"] == "Your Treadwell proposal — Westport"
    assert "Getting you on the schedule" in captured["html"], "the shipped heading is gone"
    assert "Review and approve" in captured["html"]


def test_saved_wording_replaces_it(captured):
    tpl = {"next_steps": {"subject": "Following up on {project}", "title": "A nudge",
                          "body": "Hi {first_name},\n\nWe need {need}.\n\n{link}\n\nThanks!",
                          "cta": "Open the proposal"}}
    email_sender.send_followup("a@b.com", "https://x/p/tok", "Westport", "next_steps",
                               name="Dave Smith", templates=tpl)
    h = captured["html"]
    # A per-template subject is no longer part of the model. A stored template from before
    # the change still parses; its subject is ignored rather than migrated, and this says so.
    assert captured["subject"] == "Your Treadwell proposal — Westport"
    assert "A nudge" in h, "the saved HEADING is what a template still controls"
    assert "Hi Dave," in h, "first name not substituted"
    assert "signed approval and the deposit" in h, "{need} not substituted"
    assert "Open the proposal" in h, "custom button label not used"
    assert "{link}" not in h and "{project}" not in h, "a raw token reached the email"


def test_the_deposit_phrase_still_follows_the_job(captured):
    """{need} exists because promising a deposit on a job sent without one would be wrong, and GC
    work usually is. An edited template must not lose that."""
    tpl = {"checkin": {"title": "t", "body": "We need {need}. {link}", "cta": "c"}}
    email_sender.send_followup("a@b.com", "u", "P", "checkin", templates=tpl,
                               deposit_required=False)
    assert "signed approval" in captured["html"]
    assert "deposit" not in captured["html"].split("signed approval")[1][:40]


def test_plain_text_becomes_paragraphs(captured):
    tpl = {"checkin": {"title": "t",
                       "body": "One.\n\nTwo.\n\n{link}\n\nThree.", "cta": "c"}}
    email_sender.send_followup("a@b.com", "u", "P", "checkin", templates=tpl)
    assert captured["html"].count("<p>") >= 3


def test_a_partial_saved_template_falls_back_for_that_email_only(captured):
    """Only three of four edited, or one cleared: the rest must still send properly."""
    email_sender.send_followup("a@b.com", "u", "Westport", "checkin",
                               templates={"next_steps": {"body": "{link}"}})
    assert "Checking in" in captured["html"], "should have used the shipped wording"
    assert captured["subject"] == "Your Treadwell proposal — Westport"


def test_the_status_ask_survives_saved_wording(captured):
    """The "delayed / not moving forward" escape hatch is what stops the recurring series being a
    dead end for the customer. An edited template must not drop it."""
    tpl = {"checkin": {"title": "t", "body": "{link}", "cta": "c"}}
    email_sender.send_followup("a@b.com", "u", "P", "checkin", templates=tpl,
                               token="tok", include_status_ask=True)
    assert "tok" in captured["html"]


# ── the preview ───────────────────────────────────────────────────────
def test_the_preview_fills_every_token():
    """The editor's whole safety net: an unfilled token or a missing button is obvious here and
    invisible in the form."""
    p = fs.preview(fs.defaults(), "next_steps")
    assert "subject" not in p, "the preview is showing a subject a template no longer owns"
    for token in fs.TOKENS:
        assert token not in p["title"] and token not in p["body"], token
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
    # A save reads what is saved first (it is the base the payload lays over). Stubbed as absent
    # so no test pays a pool timeout.
    monkeypatch.setattr(portal_main, "_saved_followup_settings", lambda: None)


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
        # Through the row's jsonb, worked out once per row (fix round #26: `pj.j`).
        assert "pj.j ->> '%s'" % col in sql, "%s is not read at all any more" % col
    assert "cross join lateral (select to_jsonb(p) as j offset 0) pj" in sql
    # The columns that have always existed stay direct — the indirection is only for the new ones,
    # and turning the whole select into jsonb lookups would hide a genuine typo.
    assert "p.proposal_status" in sql and "p.viewed_at" in sql


def test_an_unreadable_cadence_does_not_take_the_whole_board_down(monkeypatch):
    """The same mistake as the click columns would have been. Falling back to the shipped cadence
    is what the worker does too."""
    row = {"proposal_id": "p1", "token": "t", "customer_email": "c@x.com",
           "proposal_status": "sent", "deposit_status": "pending", "schedule_status": "pending",
           "created_at": NOW}
    monkeypatch.setattr(portal_main.db, "unread_counts", lambda: {})
    monkeypatch.setattr(portal_main.db, "list_all_portal_proposals", lambda: [row])
    monkeypatch.setattr(portal_main.db, "get_settings",
                        lambda k: (_ for _ in ()).throw(db.SettingsUnreadable("reset")))
    r = client.get("/api/admin/pipeline")
    assert r.status_code == 200 and len(r.json()["proposals"]) == 1
