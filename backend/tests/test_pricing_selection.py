"""Multi-select approval math (proposals.resolve_selection + deposit_amount).
The customer-accepted total and the 25% deposit come straight from these, so the
server never trusts a client-supplied sum."""
import pytest

import proposals

DATA = {"rooms": [
    {"name": "Base", "is_base": True, "bid": {"total": 100.0}},
    {"name": "Upgrade", "is_base": False, "bid": {"total": 150.0}},
    {"name": "Add Room", "is_base": False, "bid": {"total": 40.0}},
]}


def test_single_selection_sums_to_that_option():
    chosen, total = proposals.resolve_selection(DATA, ["Base"])
    assert [o["label"] for o in chosen] == ["Base"]
    assert total == 100.0


def test_multi_selection_sums_server_side():
    chosen, total = proposals.resolve_selection(DATA, ["Base", "Add Room"])
    assert total == 140.0
    assert [o["label"] for o in chosen] == ["Base", "Add Room"]


def test_unknown_label_rejected():
    with pytest.raises(ValueError):
        proposals.resolve_selection(DATA, ["Base", "Ghost Option"])


def test_duplicate_label_rejected():
    with pytest.raises(ValueError):
        proposals.resolve_selection(DATA, ["Base", "Base"])


def test_empty_selection_rejected():
    with pytest.raises(ValueError):
        proposals.resolve_selection(DATA, [])


def test_blank_label_rejected():
    with pytest.raises(ValueError):
        proposals.resolve_selection(DATA, ["  "])


def test_deposit_is_25_percent_rounded():
    assert proposals.deposit_amount(100.0) == 25.0
    assert proposals.deposit_amount(21937.0) == 5484.25
    assert proposals.deposit_amount(None) is None


def test_deposit_of_summed_multi_selection():
    _chosen, total = proposals.resolve_selection(DATA, ["Base", "Upgrade"])   # 250
    assert proposals.deposit_amount(total) == 62.5
