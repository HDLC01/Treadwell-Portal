"""Pricing-option extraction is the most safety-critical pure logic: the approved
total a customer accepts comes straight from it. These tests pin its behavior
across the real draft shapes (per-room, base-only, alternate, add-ons, empty)."""
import proposals


def test_pricing_options_rooms_multi():
    data = {"rooms": [
        {"name": "Base", "is_base": True, "bid": {"total": 100.0}, "system_desc": "Epoxy"},
        {"name": "Upgrade", "is_base": False, "base_total": 100.0, "show_diff": True, "bid": {"total": 150.0}},
    ]}
    opts = proposals.pricing_options(data)
    assert [o["label"] for o in opts] == ["Base", "Upgrade"]
    assert opts[0]["total"] == 100.0 and opts[0]["is_base"] is True
    assert opts[1]["total"] == 150.0 and opts[1]["diff"] == 50.0


def test_pricing_options_base_only():
    data = {"computed_bid": {"full_bid": {"total_base_bid": 21937}}, "system_name": "Matte"}
    opts = proposals.pricing_options(data)
    assert len(opts) == 1
    assert opts[0]["label"] == "Base Bid" and opts[0]["total"] == 21937.0


def test_pricing_options_alternate_appended():
    data = {"computed_bid": {"full_bid": {"total_base_bid": 100}},
            "alternate_computed_bid": {"full_bid": {"total_base_bid": 200}}, "alternate_label": "Alt"}
    opts = proposals.pricing_options(data)
    assert any(o["label"] == "Alt" and o["total"] == 200.0 for o in opts)


def test_pricing_options_empty():
    assert proposals.pricing_options({}) == []


def test_addons_filters_blank_and_null():
    data = {"price_lines": [{"label": "X", "amount": 50}, {"label": "", "amount": 10}, {"label": "Y", "amount": None}]}
    assert proposals.addons(data) == [{"label": "X", "amount": 50.0}]


def test_build_view_model_shape():
    row = {"customer_name": "Jo", "proposal_status": "sent", "deposit_status": "pending",
           "schedule_status": "pending", "approved_name": None, "approved_title": None,
           "approved_date": None, "approved_total": None, "approved_option": None, "pdf_path": None,
           "project_name": None}
    data = {"project_name": "P", "computed_bid": {"full_bid": {"total_base_bid": 100}}}
    vm = proposals.build_view_model(row, data)
    assert vm["project_name"] == "P"
    assert vm["status"] == {"proposal": "sent", "deposit": "pending", "schedule": "pending"}
    assert len(vm["options"]) == 1 and vm["options"][0]["total"] == 100.0
    assert vm["has_pdf"] is False
