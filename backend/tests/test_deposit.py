"""Deposit reference code (proposals.deposit_ref) — the human-matchable memo
reference staff use to reconcile a customer's bank transfer. Pure logic."""
import proposals


def test_ref_first_six_alnum_uppercased():
    assert proposals.deposit_ref("8dbe3385-be1d-4081-bdd5-96a51868187d") == "TW-8DBE33"


def test_ref_strips_non_alnum():
    assert proposals.deposit_ref("---a.b_c9---") == "TW-ABC9"   # dashes/dots/underscores dropped


def test_ref_is_stable():
    pid = "43f891da-bb9a-40c9-b927-0788058317d9"
    assert proposals.deposit_ref(pid) == proposals.deposit_ref(pid)


def test_ref_empty_or_none_falls_back():
    assert proposals.deposit_ref("") == "TW-DEPOSIT"
    assert proposals.deposit_ref(None) == "TW-DEPOSIT"
    assert proposals.deposit_ref("----") == "TW-DEPOSIT"
