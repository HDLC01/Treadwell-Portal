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


DEPOSIT_PCT = 0.25   # V1: deposit invoice is 25% of the approved total


def deposit_amount(total) -> float | None:
    t = _num(total)
    return None if t is None else round(t * DEPOSIT_PCT, 2)


def deposit_ref(proposal_id: str) -> str:
    """Short, human-matchable reference for the customer's bank-transfer memo, so
    staff can match the deposit on the bank statement at a glance. Deterministic
    from the proposal id (not secret)."""
    alnum = "".join(ch for ch in (proposal_id or "") if ch.isalnum())
    return ("TW-" + alnum[:8].upper()) if alnum else "TW-DEPOSIT"   # 8 chars → ~256x fewer collisions than 6


def resolve_selection(data: dict[str, Any], labels) -> tuple[list[dict[str, Any]], float]:
    """Validate a customer's chosen option labels against the published pricing
    options and sum SERVER-SIDE. Returns (chosen_options, total). Raises
    ValueError on empty / unknown / duplicate labels. The total is always the
    server's — a client-supplied total is never trusted."""
    by_label: dict[str, dict[str, Any]] = {}
    for o in pricing_options(data):
        by_label.setdefault(o["label"], o)   # first wins if the source repeats a label
    if not labels:
        raise ValueError("no_selection")
    seen: set[str] = set()
    chosen: list[dict[str, Any]] = []
    for raw in labels:
        lbl = (raw or "").strip()
        if not lbl:
            raise ValueError("invalid_option")
        if lbl in seen:
            raise ValueError("duplicate_option")
        opt = by_label.get(lbl)
        if opt is None:
            raise ValueError("unknown_option")
        seen.add(lbl)
        chosen.append(opt)
    total = round(sum(o["total"] for o in chosen), 2)
    return chosen, total


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
            "contacts": proposal_row.get("contacts_status") or "pending",
            "schedule": proposal_row.get("schedule_status"),
        },
        "approved": {
            "name": proposal_row.get("approved_name"),
            "title": proposal_row.get("approved_title"),
            "date": proposal_row.get("approved_date").isoformat() if proposal_row.get("approved_date") else None,
            "total": _num(proposal_row.get("approved_total")),
            "option": proposal_row.get("approved_option"),        # denormalized ", "-joined summary
            "options": proposal_row.get("approved_options"),      # jsonb label list (None on pre-revamp rows)
            "deposit_amount": _num(proposal_row.get("deposit_amount")),
        },
        "has_pdf": bool(proposal_row.get("pdf_path")),
    }
