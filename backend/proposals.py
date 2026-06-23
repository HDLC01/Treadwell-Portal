"""Turn a proposal-tool `drafts.data` blob into the portal's view model.

The proposal tool stores everything in one JSONB blob. Pricing can be a single
base bid, multiple per-room options (`rooms[]`), and/or an alternate system
(`alternate_computed_bid`), plus optional add-on lines (`price_lines[]`). The
portal needs a clean list of selectable options so the approved total is
unambiguous.
"""
from __future__ import annotations

from typing import Any


def _num(v) -> float | None:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def pricing_options(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Selectable pricing options (each has a label + an all-in total)."""
    options: list[dict[str, Any]] = []
    rooms = data.get("rooms") or []
    if isinstance(rooms, list) and rooms:
        for r in rooms:
            bid = r.get("bid") or {}
            total = _num(bid.get("total"))
            if total is None:
                continue
            opt = {
                "label": r.get("name") or ("Base Bid" if r.get("is_base") else "Option"),
                "total": total,
                "system_desc": r.get("system_desc") or "",
                "is_base": bool(r.get("is_base")),
            }
            base_total = _num(r.get("base_total"))
            if r.get("show_diff") and base_total is not None:
                opt["diff"] = round(total - base_total, 2)
            options.append(opt)
    else:
        base = ((data.get("computed_bid") or {}).get("full_bid") or {})
        total = _num(base.get("total_base_bid"))
        if total is not None:
            options.append({"label": "Base Bid", "total": total,
                            "system_desc": data.get("system_name") or "", "is_base": True})

    alt = data.get("alternate_computed_bid") or {}
    alt_total = _num((alt.get("full_bid") or {}).get("total_base_bid")) if alt else None
    if alt_total is not None:
        options.append({"label": data.get("alternate_label") or "Alternate", "total": alt_total,
                        "system_desc": "", "is_base": False})
    return options


def addons(data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for line in (data.get("price_lines") or []):
        amt = _num(line.get("amount"))
        if line.get("label") and amt is not None:
            out.append({"label": line["label"], "amount": amt})
    return out


def build_view_model(proposal_row: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_name": data.get("project_name") or proposal_row.get("project_name") or "Your Proposal",
        "city_state": data.get("city_state") or "",
        "customer_name": proposal_row.get("customer_name") or data.get("contact_name") or "",
        "summary": {
            "system_name": data.get("system_name") or "",
            "texture": data.get("texture") or "",
            "scope_notes": data.get("scope_notes") or "",
            "schedule_notes": data.get("schedule_notes") or "",
            "exclusions": data.get("exclusions") or "",
            "proposal_date": data.get("proposal_date") or "",
            "site_visit_date": data.get("site_visit_date") or "",
        },
        "options": pricing_options(data),
        "addons": addons(data),
        "status": {
            "proposal": proposal_row.get("proposal_status"),
            "deposit": proposal_row.get("deposit_status"),
            "schedule": proposal_row.get("schedule_status"),
        },
        "approved": {
            "name": proposal_row.get("approved_name"),
            "title": proposal_row.get("approved_title"),
            "date": proposal_row.get("approved_date").isoformat() if proposal_row.get("approved_date") else None,
            "total": _num(proposal_row.get("approved_total")),
            "option": proposal_row.get("approved_option"),
        },
        "has_pdf": bool(proposal_row.get("pdf_path")),
    }
