"""A contact can be left out of the automated follow-ups without being left out of the proposal.

Hanz, 2026-08-12:

    "just like the 25% deposit creat a checkbox for each contact if they will be able to receive
     the automated follow ups or no"
    "Then on this project container on the follow ups we must have the ability to add or remove
     COntacts who receive the follow ups."

The case it exists for: an accounts-payable address that should get the proposal and the invoice
but not four chasing emails, or a second contact who asked not to be nagged.

"REMOVE" MEANS STOP CHASING, NOT STOP BEING A CONTACT. The row stays. They keep receiving the
proposal, the invoice, milestone mail and replies — only the cadence skips them. Deleting the
recipient would revoke their portal access, which is a far larger decision than "don't nag this
person", and is what `db.remove_recipient` is for.

THE DIRECTION OF EVERY FALLBACK IS THE SAME: toward still chasing.

  * the column is NOT NULL DEFAULT TRUE, so every recipient that existed before the migration
    keeps being chased (verified on prod: 11 rows, 11 opted in);
  * the flag is read through to_jsonb, so a database without the migration answers null, which
    reads as opted IN. Reading a missing column as "off" would silently stop chasing every live
    bid in the system — the worst outcome available here, and the reason this is not a plain
    `where followups` in SQL;
  * a FAILED read falls back to the primary contact, exactly as the worker behaved before.

The one case that deliberately sends NOTHING is every contact opting out, because that is a
decision somebody made rather than a gap in the data. Distinguishing it from a failed read is what
most of this file is about.
"""
import inspect

import pytest

import db
import followup_worker
import main

PID = "pid-0001"
A = "dana.reed@acme.com"      # the estimator's contact
B = "ap@acme.com"             # accounts payable — wants the invoice, not the chasing
PROP = {"proposal_id": PID, "token": "tok-abc-123", "project_name": "Westport",
        "customer_email": A, "proposal_status": "sent", "deposit_required": True}


# ── the worker chases only the opted-in ──────────────────────────────────────
def test_an_opted_out_contact_is_not_chased(monkeypatch):
    monkeypatch.setattr(db, "get_followup_recipients", lambda pid: [A])
    monkeypatch.setattr(db, "get_recipients", lambda pid: [A, B])
    assert followup_worker._customer_recipients(dict(PROP)) == [A]


def test_the_opted_out_contact_is_still_a_recipient(monkeypatch):
    """The whole distinction. They keep the proposal, the invoice and every reply — the cadence
    is the only thing that skips them."""
    monkeypatch.setattr(db, "get_recipients", lambda pid: [A, B])
    monkeypatch.setattr(db, "get_followup_recipients", lambda pid: [A])
    assert B in db.get_recipients(PID)
    assert B not in followup_worker._customer_recipients(dict(PROP))


def test_the_worker_asks_for_the_followup_list_not_the_recipient_list(monkeypatch):
    """Asserted against the source as well as the behaviour: swapping the call back to
    get_recipients passes every behavioural test that stubs both, and chases everybody."""
    src = inspect.getsource(followup_worker._customer_recipients)
    assert "db.get_followup_recipients" in src
    assert "db.get_recipients(p[\"proposal_id\"])" in src, (
        "the empty-vs-all-opted-out distinction below needs the plain list too")


def test_everybody_opting_out_sends_nothing(monkeypatch):
    """A decision, not a gap. Falling back to the primary here would chase the one person who
    was explicitly excluded."""
    monkeypatch.setattr(db, "get_followup_recipients", lambda pid: [])
    monkeypatch.setattr(db, "get_recipients", lambda pid: [A, B])
    assert followup_worker._customer_recipients(dict(PROP)) == []


def test_a_proposal_with_no_recipient_rows_still_falls_back_to_the_primary(monkeypatch):
    """Empty because there is nothing there, not because somebody opted out. Legacy proposals
    predate the recipients table entirely and must keep being chased."""
    monkeypatch.setattr(db, "get_followup_recipients", lambda pid: [])
    monkeypatch.setattr(db, "get_recipients", lambda pid: [])
    assert followup_worker._customer_recipients(dict(PROP)) == [A]


def test_a_failed_read_behaves_exactly_as_the_worker_did_before(monkeypatch):
    def boom(pid):
        raise RuntimeError("postgrest down")
    monkeypatch.setattr(db, "get_followup_recipients", boom)
    monkeypatch.setattr(db, "get_recipients", boom)
    assert followup_worker._customer_recipients(dict(PROP)) == [A]


def test_a_failed_second_read_does_not_crash_the_tick(monkeypatch):
    """The all-opted-out check reads the plain list. If THAT fails, the worker must still pick a
    recipient rather than raising out of a background loop."""
    monkeypatch.setattr(db, "get_followup_recipients", lambda pid: [])
    monkeypatch.setattr(db, "get_recipients",
                        lambda pid: (_ for _ in ()).throw(RuntimeError("down")))
    assert followup_worker._customer_recipients(dict(PROP)) == [A]


def test_the_staff_reminder_is_untouched(monkeypatch):
    """Opting a CUSTOMER out of chasing must not stop the estimator being told the bid is
    stalling — that is an internal nudge about their own work."""
    src = inspect.getsource(followup_worker._staff_recipients)
    assert "followup" not in src.replace("_staff_recipients", ""), (
        "the staff path now consults the customer opt-out flag")


# ── reading the flag ─────────────────────────────────────────────────────────
def test_a_missing_column_reads_as_opted_IN():
    """to_jsonb rather than a bare column, and `!= "false"` rather than truthiness. A database
    without the migration answers null for every row; treating that as "off" would silently stop
    chasing every live bid in the system."""
    src = inspect.getsource(db.get_followup_recipients)
    assert "to_jsonb(r) ->> 'followups'" in src
    assert '!= "false"' in src, "the flag is not read in the opted-in-by-default direction"


def test_the_reader_keeps_the_same_order_as_everything_else():
    """A contact list that reorders between the drawer and the cadence reads as two lists."""
    assert "order by added_at, id" in inspect.getsource(db.get_followup_recipients)
    assert "order by added_at, id" in inspect.getsource(db.get_recipients)


def test_the_toggle_is_case_insensitive_and_reports_a_miss():
    """Recipient lists are typed by hand. A toggle that silently matched nothing would read as
    working while the contact kept being chased."""
    src = inspect.getsource(db.set_followup_recipient)
    assert "lower(email) = lower(%s)" in src
    assert "returning id" in src, "a no-op update cannot be distinguished from a real one"


# ── the send page writes it ──────────────────────────────────────────────────
def test_the_publish_payload_is_validated_like_the_recipient_list():
    """A malformed entry that was ignored rather than refused would mean somebody un-ticked a box
    and got chased anyway — invisible until a customer complains."""
    src = inspect.getsource(main.admin_publish)
    assert '_clean_emails(body.get("no_followups"))' in src
    i = src.index('_clean_emails(body.get("no_followups"))')
    assert "return _json" in src[i:i + 200], "a bad no_followups list is not refused"


def test_the_flag_is_written_in_the_same_transaction_as_the_recipients():
    """Two writes would leave a window in which the contact exists and is due a chase. The worker
    runs on its own clock and does not wait for a second statement."""
    assert "no_followups=no_followups" in inspect.getsource(main.admin_publish)
    src = inspect.getsource(db.set_recipients)
    assert "with pool().connection() as conn:" in src
    assert "set followups" in src
    assert src.index("with pool().connection()") < src.index("set followups")


def test_every_row_gets_an_explicit_value_not_only_the_unticked_ones():
    """Retained rows keep their value `on conflict do nothing`. Writing only the un-ticked ones
    would make an earlier opt-out stick after somebody ticked the box again and re-sent."""
    src = inspect.getsource(db.set_recipients)
    assert "(e not in off, proposal_id, e)" in src, (
        "the flag is not written for every recipient, so a re-tick would not take effect")


def test_a_legacy_publish_leaves_the_flags_alone():
    """None means the caller said nothing about follow-ups. Defaulting it to "chase everybody"
    would undo a deliberate opt-out the first time an older client published."""
    src = inspect.getsource(db.set_recipients)
    assert "if no_followups is not None:" in src


# ── the drawer endpoint ──────────────────────────────────────────────────────
def test_the_endpoint_needs_the_service_token():
    src = inspect.getsource(main.admin_followup_recipient)
    assert "_admin_ok(request)" in src
    assert src.index("_admin_ok") < src.index("db.set_followup_recipient")


def test_turning_a_contact_off_does_not_delete_them():
    """The single most important line in the endpoint. remove_recipient revokes portal access;
    this is meant to stop the nagging."""
    src = inspect.getsource(main.admin_followup_recipient)
    assert "remove_recipient" not in src, (
        "the endpoint deletes the recipient, which revokes their access to the proposal")


def test_a_newly_added_contact_is_sent_the_link():
    """The link IS the access. Adding somebody without sending it puts a contact on the list who
    can never open the thing they are a contact for."""
    src = inspect.getsource(main.admin_followup_recipient)
    # Inside the ADD branch specifically. A no-op stand-in for the send survived an assertion
    # that only looked for the name somewhere in the function.
    i = src.index("if add and email not in existing:")
    j = src.index("elif email not in existing:")
    add_branch = src[i:j]
    assert "email_sender.send_portal_link(" in add_branch, (
        "a newly added contact is never sent the link, so they cannot reach the proposal they "
        "are now a contact for")
    assert "except Exception" in add_branch, (
        "a mail failure would undo an add that already succeeded")


def test_a_stranger_cannot_be_toggled_without_being_added():
    """The refusal has to be its own branch BEFORE the write. Merely asserting the string appears
    passed with the branch deleted, because the same error is returned again lower down when the
    update matches no row — and by then add-a-stranger has already been decided."""
    src = inspect.getsource(main.admin_followup_recipient)
    assert "elif email not in existing:" in src, (
        "toggling an address that is not on the proposal no longer stops early")
    i = src.index("elif email not in existing:")
    assert '"not_a_recipient"' in src[i:i + 200]
    assert i < src.index("db.set_followup_recipient"), (
        "the stranger check runs after the write")


def test_the_recipient_cap_still_applies():
    src = inspect.getsource(main.admin_followup_recipient)
    assert "MAX_RECIPIENTS" in src


def test_a_malformed_address_is_refused():
    src = inspect.getsource(main.admin_followup_recipient)
    assert "_EMAIL_RE.match(email)" in src and "254" in src


def test_absent_enabled_means_ON():
    """The drawer's Add flow posts no `enabled`. Defaulting to off would add a contact and
    immediately exclude them, which is not what "add" means."""
    assert 'body.get("enabled") is not False' in inspect.getsource(main.admin_followup_recipient)


# ── the drawer can see it ────────────────────────────────────────────────────
def test_recipient_activity_says_who_is_being_chased(monkeypatch):
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: [A, B])
    monkeypatch.setattr(main.db, "get_followup_recipients", lambda pid: [A])
    monkeypatch.setattr(main.db, "list_views", lambda pid: [])
    monkeypatch.setattr(main.db, "list_messages", lambda pid, after=0: [])
    monkeypatch.setattr(main.db, "list_deposits", lambda pid: [])
    got = {r["email"]: r for r in main._recipient_activity(PID, dict(PROP), None)}
    assert got[A]["followups"] is True
    assert got[B]["followups"] is False


def test_an_unreadable_flag_shows_everyone_as_chased(monkeypatch):
    """Which is what a database without the migration actually does. Showing every contact as
    opted OUT would tell an estimator nobody is being chased when everybody is."""
    monkeypatch.setattr(main.db, "get_recipients", lambda pid: [A, B])
    monkeypatch.setattr(main.db, "get_followup_recipients",
                        lambda pid: (_ for _ in ()).throw(RuntimeError("no column")))
    monkeypatch.setattr(main.db, "list_views", lambda pid: [])
    monkeypatch.setattr(main.db, "list_messages", lambda pid, after=0: [])
    monkeypatch.setattr(main.db, "list_deposits", lambda pid: [])
    got = {r["email"]: r for r in main._recipient_activity(PID, dict(PROP), None)}
    assert got[A]["followups"] is True and got[B]["followups"] is True


# ── the migration ────────────────────────────────────────────────────────────
def test_the_ddl_defaults_to_being_chased_and_is_additive():
    import pathlib
    schema = (pathlib.Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8")
    assert "add column if not exists followups boolean not null default true" in schema, (
        "the flag must default to TRUE, or applying it stops every live bid being chased")
    tail = schema[schema.index("add column if not exists followups"):]
    for danger in ("drop column", "drop table", "update public.portal_proposal_recipients set"):
        assert danger not in tail.lower(), "the migration is not additive: %r" % danger
