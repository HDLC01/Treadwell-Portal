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


# ── value-engineering (add/deduct) rows ──────────────────────────────────────
# A VE row is an ALTERNATIVE priced against the base bid, not an extra job: its
# `bid.total` is what the job costs if you take it. Summing it as a lump sum
# overcharged the customer, and that wrong number flowed into approved_total →
# the 25% deposit → the invoice.
VE_DATA = {"rooms": [
    {"name": "Base", "is_base": True, "bid": {"total": 10000.0}, "base_total": 10000.0},
    # cheaper alternative: 10000 - 8000 = deduct 2000
    {"name": "VE Deduct", "is_base": False, "price_mode": "deduct",
     "bid": {"total": 8000.0}, "base_total": 10000.0},
    # pricier alternative: 12500 - 10000 = add 2500
    {"name": "VE Add", "is_base": False, "price_mode": "deduct",
     "bid": {"total": 12500.0}, "base_total": 10000.0},
    # ordinary standalone option, unaffected
    {"name": "Extra Room", "is_base": False, "bid": {"total": 1500.0}, "base_total": 10000.0},
]}


def test_ve_rows_expose_mode_and_signed_delta():
    by = {o["label"]: o for o in proposals.pricing_options(VE_DATA)}
    assert by["VE Deduct"]["price_mode"] == "deduct"
    assert by["VE Deduct"]["delta"] == -2000.0     # negative → "Deduct ($2,000)"
    assert by["VE Add"]["delta"] == 2500.0         # positive → "Add $2,500"
    assert by["Extra Room"]["price_mode"] == "total"
    assert by["Base"]["price_mode"] == "total"


def test_base_plus_ve_deduct_subtracts_from_base():
    _chosen, total = proposals.resolve_selection(VE_DATA, ["Base", "VE Deduct"])
    assert total == 8000.0        # NOT 18000 — the VE replaces the base price


def test_base_plus_ve_add_adds_to_base():
    _chosen, total = proposals.resolve_selection(VE_DATA, ["Base", "VE Add"])
    assert total == 12500.0       # NOT 22500


def test_ve_alone_still_implies_the_base_bid():
    """An add/deduct on its own is meaningless — the base is folded in so the
    customer can never approve a bare delta."""
    chosen, total = proposals.resolve_selection(VE_DATA, ["VE Deduct"])
    assert total == 8000.0
    assert [o["label"] for o in chosen] == ["Base", "VE Deduct"]


def test_lump_sum_options_still_sum_normally():
    _chosen, total = proposals.resolve_selection(VE_DATA, ["Base", "Extra Room"])
    assert total == 11500.0


def test_ve_and_lump_sum_combine():
    _chosen, total = proposals.resolve_selection(VE_DATA, ["Base", "VE Deduct", "Extra Room"])
    assert total == 9500.0        # 10000 base - 2000 VE + 1500 extra
