"""Contacts payload validation (main._clean_contacts) — pure logic. The DB
replace-set + endpoint are covered by the staging smoke, per repo convention."""
import main

f = main._clean_contacts


def test_requires_a_list():
    assert f(None)[1] == "no_contacts"
    assert f([])[1] == "no_contacts"
    assert f("nope")[1] == "no_contacts"


def test_primary_required():
    out, err = f([{"role": "other", "name": "Sam"}])
    assert out is None and err == "primary_required"


def test_name_required():
    assert f([{"role": "primary", "name": "  "}])[1] == "name_required"


def test_valid_primary_only():
    out, err = f([{"role": "primary", "name": "Sam Lane", "email": "SAM@x.com", "phone": "555-1"}])
    assert err is None
    assert out == [{"role": "primary", "name": "Sam Lane", "email": "sam@x.com", "phone": "555-1", "label": None}]


def test_invalid_email_rejected():
    assert f([{"role": "primary", "name": "Sam", "email": "not-an-email"}])[1] == "invalid_email"


def test_blank_email_becomes_none():
    out, err = f([{"role": "primary", "name": "Sam", "email": "  "}])
    assert err is None and out[0]["email"] is None


def test_invalid_role_rejected():
    assert f([{"role": "ceo", "name": "Sam"}])[1] == "invalid_role"


def test_multi_contacts_with_ap():
    out, err = f([
        {"role": "primary", "name": "Sam"},
        {"role": "accounts_payable", "name": "Pat", "email": "ap@x.com"},
    ])
    assert err is None
    assert [c["role"] for c in out] == ["primary", "accounts_payable"]


def test_cap_enforced():
    too_many = [{"role": "primary", "name": "P"}] + [{"role": "other", "name": f"c{i}"} for i in range(main.MAX_CONTACTS)]
    assert f(too_many)[1] == "too_many"
