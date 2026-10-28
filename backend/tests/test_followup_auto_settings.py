"""The follow-up SCHEDULE as settings (2026-10-28): the shape, the refusals, the migration.

The trigger: an admin typed 0 into "Then every", "Tell the estimator after" and "Stop after" on
prod's Auto Followups page, and the old module silently replaced the zeros with defaults, so prod
saved daily check-ins up to 20. Now:

  * the page has five emails, each with an ON/OFF switch and a number of days;
  * a number of days outside 1..90 (0 included) is REFUSED with a sentence naming the box;
  * the page is shown what was saved;
  * an old saved row is migrated on read, without error (the live prod row is pinned below).
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import followup_rules as fr
import followup_settings as fs
import followup_worker as W
import main

NOW = datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc)      # noon Chicago
D = timedelta(days=1)

# The live prod row, 2026-10-28 (after the stopgap): what merge() must read without error.
PROD_ROW = {
    "first_nudge_hours": 72, "second_nudge_hours": 168, "recurring_hours": 2160,
    "max_recurring": 1, "staff_personal_hours": 24,
    "thread_subject": "Your Treadwell proposal — {project}",
    "templates": {
        "not_viewed": {"title": "Your proposal is waiting",
                       "body": "Hi {first_name},\n\nIt is here.\n\n{link}", "cta": "View"},
        "checkin": {"title": "Checking in", "body": "Still open?\n\n{link}", "cta": "View"},
        "deposit_nudge": {"title": "Deposit", "body": "Deposit please\n\n{link}", "cta": "Pay"},
    },
}

SCHEDULE = {k: fs.DEFAULTS[k] for k in fs.SWITCHES + fs.DAY_FIELDS}


# ── the shape and the defaults ────────────────────────────────────────────────
def test_the_defaults_are_the_approved_page():
    assert SCHEDULE == {
        "not_opened_first_on": True, "not_opened_first_days": 3,
        "not_opened_second_on": True, "not_opened_second_days": 7,
        "opened_first_on": True, "opened_first_days": 3,
        "opened_second_on": True, "opened_second_days": 7,
        "estimator_on": True, "estimator_days": 1,
    }


def test_nothing_repeats_any_more():
    out = fs.defaults()
    for gone in fs.LEGACY_KEYS:
        assert gone not in out, gone
    assert "checkin" not in fs.TEMPLATE_KEYS and "deposit_nudge" not in fs.TEMPLATE_KEYS
    assert list(fs.LABELS) == ["sent", "not_viewed", "not_viewed_2", "next_steps", "second_nudge"]


# ── refusals: never replaced, never clamped ───────────────────────────────────
@pytest.mark.parametrize("field", fs.DAY_FIELDS)
@pytest.mark.parametrize("bad", [0, -1, 91, 365, "", None, "abc", "3.5", 2.5, True])
def test_days_out_of_range_are_refused_with_a_sentence(field, bad):
    with pytest.raises(fs.ValidationError) as e:
        fs.validate({field: bad})
    assert "1 to 90 days" in str(e.value)


def test_each_refusal_names_its_own_box():
    sentences = {}
    for field in fs.DAY_FIELDS:
        with pytest.raises(fs.ValidationError) as e:
            fs.validate({field: 0})
        sentences[field] = str(e.value)
    assert sentences["not_opened_second_days"] == \
        "Not opened yet: the 2nd email must be 1 to 90 days after the 1st."
    assert sentences["opened_second_days"] == \
        "Opened, no decision: the 2nd email must be 1 to 90 days after the 1st."
    assert sentences["estimator_days"] == \
        "Tell the estimator must be 1 to 90 days after the last email."
    assert len(set(sentences.values())) == len(sentences)


def test_zero_is_never_off():
    """The page has a switch for off. 0 days is a mistake, refused, and the switch is untouched."""
    with pytest.raises(fs.ValidationError):
        fs.validate({"opened_first_days": 0, "opened_first_on": True})


@pytest.mark.parametrize("given,expect", [(1, 1), (90, 90), ("7", 7), (" 14 ", 14), (5.0, 5)])
def test_whole_days_in_range_are_kept_exactly(given, expect):
    assert fs.validate({"opened_second_days": given})["opened_second_days"] == expect


@pytest.mark.parametrize("given,expect", [(True, True), (False, False), ("on", True),
                                          ("off", False), ("true", True), (0, False)])
def test_switches_are_on_or_off(given, expect):
    assert fs.validate({"estimator_on": given})["estimator_on"] is expect


def test_a_switch_that_is_neither_is_refused():
    with pytest.raises(fs.ValidationError) as e:
        fs.validate({"not_opened_first_on": "maybe"})
    assert "switched on or off" in str(e.value)


def test_a_window_that_ends_before_it_starts_is_refused_not_replaced():
    with pytest.raises(fs.ValidationError):
        fs.validate({"send_start_hour": 18, "send_end_hour": 8})
    with pytest.raises(fs.ValidationError):
        fs.validate({"send_start_hour": 8, "send_end_hour": 25})


def test_a_partial_save_keeps_what_is_saved():
    saved = fs.validate({"opened_first_days": 5, "estimator_on": False})
    again = fs.validate({"not_opened_first_days": 2}, saved)
    assert (again["opened_first_days"], again["estimator_on"],
            again["not_opened_first_days"]) == (5, False, 2)


def test_an_old_page_posting_the_old_shape_is_told_to_reload():
    """Its numbers would otherwise be ignored in silence, the bug this rework exists to end."""
    with pytest.raises(fs.ValidationError) as e:
        fs.validate({"first_nudge_hours": 72, "recurring_hours": 0, "max_recurring": 0})
    assert "reload" in str(e.value)


# ── the migration on read ─────────────────────────────────────────────────────
def test_the_live_prod_row_migrates_on_read_without_error():
    got = fs.merge(PROD_ROW)
    assert {k: got[k] for k in SCHEDULE} == {
        "not_opened_first_on": True, "not_opened_first_days": 3,
        "not_opened_second_on": True, "not_opened_second_days": 7,
        "opened_first_on": True, "opened_first_days": 3,
        "opened_second_on": True, "opened_second_days": 7,
        "estimator_on": True, "estimator_days": 1,
    }
    for gone in fs.LEGACY_KEYS:
        assert gone not in got, "%s came back out of the migration" % gone
    assert got["thread_subject"] == PROD_ROW["thread_subject"]
    assert got["templates"]["not_viewed"]["body"] == PROD_ROW["templates"]["not_viewed"]["body"]
    # The retired emails' wording is kept for the old tasks that still carry their names.
    assert got["templates"]["checkin"]["body"] == "Still open?\n\n{link}"
    assert got["templates"]["deposit_nudge"]["title"] == "Deposit"
    # Saving the migrated row back is a no-op round trip.
    assert fs.validate(got, got) == got


@pytest.mark.parametrize("hours,days", [(24, 1), (25, 2), (72, 3), (168, 7), (1, 1),
                                        (24 * 400, 90)])
def test_old_hours_become_whole_days_rounded_up(hours, days):
    assert fs.merge({"first_nudge_hours": hours})["opened_first_days"] == days


def test_old_zeros_and_junk_migrate_to_the_defaults_without_error():
    """Prod's bad save (0s, then the stopgap's 2160h / max 1): never an exception on read."""
    got = fs.merge({"first_nudge_hours": 0, "second_nudge_hours": "x",
                    "staff_personal_hours": None, "recurring_hours": 0, "max_recurring": 0})
    assert {k: got[k] for k in SCHEDULE} == SCHEDULE


def test_a_hand_broken_new_shape_row_reads_as_defaults_for_the_broken_fields():
    got = fs.merge({"opened_first_days": 0, "opened_second_days": 500, "estimator_on": "maybe",
                    "not_opened_first_days": 4, "send_start_hour": 20, "send_end_hour": 6})
    assert got["opened_first_days"] == 3 and got["opened_second_days"] == 7
    assert got["estimator_on"] is True and got["not_opened_first_days"] == 4
    assert (got["send_start_hour"], got["send_end_hour"]) == (8, 18)


def test_a_new_shape_row_wins_over_any_old_keys_left_in_it():
    got = fs.merge({"first_nudge_hours": 240, "opened_first_days": 2})
    assert got["opened_first_days"] == 2 and got["not_opened_first_days"] == 3


# ── the templates ─────────────────────────────────────────────────────────────
def test_the_new_second_not_opened_email_has_a_shipped_wording_with_its_button():
    t = fs.DEFAULT_TEMPLATES["not_viewed_2"]
    assert "{link}" in t["body"] and fs.validate_template("not_viewed_2", t) == t


def test_old_template_names_still_validate_for_old_tasks():
    for key in fs.LEGACY_TEMPLATE_KEYS:
        assert fs.validate_template(key, fs.DEFAULT_TEMPLATES[key])
    assert fs.label("checkin") == "Recurring check-in"


# ── the endpoints ─────────────────────────────────────────────────────────────
@pytest.fixture
def api(monkeypatch):
    store = {"row": None, "saved": []}
    monkeypatch.setattr(main, "_admin_ok", lambda request: True)
    monkeypatch.setattr(main.db, "get_settings", lambda k: store["row"])
    monkeypatch.setattr(main.db, "settings_meta", lambda k: {})

    def save(k, v, by=None):
        store["saved"].append(v)
        store["row"] = v
        return {"updated_at": NOW, "updated_by": by}

    monkeypatch.setattr(main.db, "save_settings", save)
    store["client"] = TestClient(main.app)
    return store


def test_get_shows_the_migrated_prod_row_and_the_pages_vocabulary(api):
    api["row"] = PROD_ROW
    j = api["client"].get("/api/admin/settings/followups").json()
    assert j["ok"] and j["saved"] is True
    assert j["settings"]["opened_second_days"] == 7 and "recurring_hours" not in j["settings"]
    assert j["step_templates"] == {"not_opened": ["not_viewed", "not_viewed_2"],
                                   "opened": ["next_steps", "second_nudge"]}
    assert j["days_range"] == [1, 90]
    assert set(j["previews"]) == set(fs.ALL_TEMPLATE_KEYS)


def test_put_refuses_zero_with_the_sentence_and_saves_nothing(api):
    r = api["client"].put("/api/admin/settings/followups",
                          json={"settings": {"opened_second_days": 0}})
    assert r.status_code == 400
    assert r.json()["error"] == \
        "Opened, no decision: the 2nd email must be 1 to 90 days after the 1st."
    assert api["saved"] == []


def test_put_saves_and_returns_exactly_what_was_saved(api):
    body = dict(SCHEDULE, opened_first_days=5, estimator_on=False)
    r = api["client"].put("/api/admin/settings/followups",
                          json={"settings": body, "by": "will@wetreadwell.com"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["settings"]["opened_first_days"] == 5 and j["settings"]["estimator_on"] is False
    assert api["saved"][-1] == j["settings"], "the page was not shown what was stored"


def test_put_of_one_field_keeps_the_rest_of_what_is_saved(api):
    api["row"] = fs.validate(dict(SCHEDULE, not_opened_second_days=12))
    r = api["client"].put("/api/admin/settings/followups",
                          json={"settings": {"estimator_days": 4}})
    assert r.status_code == 200, r.text
    assert api["saved"][-1]["not_opened_second_days"] == 12
    assert api["saved"][-1]["estimator_days"] == 4


def test_preview_refuses_what_put_refuses(api):
    r = api["client"].post("/api/admin/settings/followups/preview",
                           json={"settings": {"not_opened_first_days": 91}})
    assert r.status_code == 400 and "1 to 90 days" in r.json()["error"]


# ── the worker and the board read the SAVED schedule ───────────────────────────
def _row(**over):
    p = {"proposal_id": "p1", "token": "t", "customer_email": "c@x.com", "customer_name": "C",
         "project_name": "Westport", "proposal_status": "viewed", "deposit_status": "pending",
         "schedule_status": "pending", "followup_enrolled_at": NOW - 10 * D,
         "cycle_viewed_at": NOW - 4 * D, "created_at": NOW - 10 * D,
         "followup_disabled_at": None, "followup_paused_until": None,
         "followup_sent_history": []}
    p.update(over)
    return p


def test_the_board_dates_come_from_the_saved_schedule(api, monkeypatch):
    monkeypatch.setattr(main.db, "unread_counts", lambda: {})
    monkeypatch.setattr(main.db, "list_all_portal_proposals", lambda: [_row()])
    monkeypatch.setattr(main, "_now_utc", lambda: NOW)
    shipped = api["client"].get("/api/admin/pipeline").json()["proposals"][0]["next_followup_at"]
    assert shipped == main._iso(NOW - D), "3 days after they opened it"
    api["row"] = dict(SCHEDULE, opened_first_days=6)
    saved = api["client"].get("/api/admin/pipeline").json()["proposals"][0]["next_followup_at"]
    assert saved == main._iso(NOW + 2 * D)
    api["row"] = dict(SCHEDULE, opened_first_on=False, opened_second_on=False)
    off = api["client"].get("/api/admin/pipeline").json()["proposals"][0]["next_followup_at"]
    assert off is None


def test_the_worker_reads_the_saved_schedule(monkeypatch):
    monkeypatch.setattr(W.db, "get_settings", lambda k: dict(SCHEDULE, opened_first_on=False))
    cfg = W._settings()
    assert cfg["opened_first_on"] is False
    p = _row(cycle_viewed_at=NOW - 4 * D)
    assert [d for d in fr.due_now(p, NOW, cfg) if d.audience == "customer"] == [], (
        "a switched-off 1st email went")


def test_the_worker_reads_the_old_prod_row_as_its_migration(monkeypatch):
    monkeypatch.setattr(W.db, "get_settings", lambda k: PROD_ROW)
    cfg = W._settings()
    assert (cfg["opened_first_days"], cfg["opened_second_days"], cfg["estimator_days"]) == (3, 7, 1)
