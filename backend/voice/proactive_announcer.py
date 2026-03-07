"""
proactive_announcer.py — Page-load voice announcements for CoPilot

Fires when the extension toggle is switched ON or a new page loads.
Builds a natural, Copilot-like spoken greeting using DOM structure only.
No LLM call — <5ms.
"""
from __future__ import annotations

import logging
import random
import re
from typing import Any

logger = logging.getLogger(__name__)


# ── Page type classifier ───────────────────────────────────────────────────────

# List/table page patterns — detected separately from form pages
_LIST_PAGE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"purchase.list|all.purchase|purchase.history|purchase.orders?(?!.form|.new|.create)", re.I), "Purchase Orders"),
    (re.compile(r"sales.list|all.sales|sales.history|orders?.list", re.I), "Sales Orders"),
    (re.compile(r"supplier.list|all.supplier|vendor.list", re.I), "Suppliers"),
    (re.compile(r"customer.list|all.customer|client.list", re.I), "Customers"),
    (re.compile(r"stock.list|inventory.list|item.list|product.list", re.I), "Inventory"),
    (re.compile(r"invoice.list|all.invoice|pending.invoice", re.I), "Invoices"),
    (re.compile(r"report|analytics|history|ledger|statement", re.I), "Report"),
]

_PAGE_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # (url+title+field pattern, short_name, copilot_description)
    (re.compile(r"purchase|purchase.order|naveen.khareedi|new.purchase|po[_\-/]", re.I),
     "Purchase Order",
     "a Purchase Order form — this is where you record incoming stock from suppliers"),

    (re.compile(r"supplier|vendor|navi[nī].sapla[yī]r", re.I),
     "Supplier",
     "a Supplier form — for adding or editing a supplier in your system"),

    (re.compile(r"customer|client|naveen.grahak|new.customer", re.I),
     "Customer",
     "a Customer form — for adding or editing a customer record"),

    (re.compile(r"sales|sell|naveen.vikri|sale[_\-/]order|sell.order", re.I),
     "Sales Order",
     "a Sales Order form — this is where you record sales to your customers"),

    (re.compile(r"stock|inventory|item.detail|item.master|warehouse", re.I),
     "Stock / Inventory",
     "an Item Details form — for managing your product stock and inventory"),

    (re.compile(r"shipping|dispatch|delivery|logistics|courier", re.I),
     "Shipping",
     "a Shipping or Dispatch form"),

    (re.compile(r"employee|staff|hr|payroll|salary", re.I),
     "HR / Employee",
     "an HR or Employee form"),

    (re.compile(r"docs\.google\.com/forms", re.I),
     "Google Form",
     "a Google Form"),

    (re.compile(r"typeform|jotform|wufoo|formstack|cognito", re.I),
     "Online Form",
     "an online form"),

    (re.compile(r"dashboard|home.?page|admin|panel|console|portal", re.I),
     "Dashboard",
     "the main dashboard"),

    (re.compile(r"register|signup|sign.up|create.account", re.I),
     "Registration",
     "a registration form"),

    (re.compile(r"login|signin|sign.in|auth", re.I),
     "Login",
     "the login page"),

    (re.compile(r"report|analytics|stats|metrics|kpi", re.I),
     "Reports",
     "a reports or analytics page"),

    (re.compile(r"settings|preferences|profile|account", re.I),
     "Settings",
     "a settings or profile page"),
]


def _detect_page_type(url: str, title: str, field_ids: list[str]) -> tuple[str, str]:
    """Returns (short_name, copilot_description)."""
    combined = f"{url} {title} {' '.join(field_ids)}".lower()
    for pattern, short, desc in _PAGE_PATTERNS:
        if pattern.search(combined):
            return short, desc
    if field_ids:
        return "Form", "a form"
    return "Page", "this page"


def _count_fields(context: dict) -> tuple[int, int, list[dict]]:
    """Returns (total_fillable, required_count, missing_required_fields)."""
    total   = 0
    missing = []
    for section in context.get("sections", []):
        for f in section.get("fields", []):
            if f.get("readonly") or f.get("calculated"):
                continue
            total += 1
            if f.get("required"):
                if not (f.get("value") and str(f["value"]).strip()):
                    missing.append(f)
    return total, len(missing) + sum(
        1 for s in context.get("sections", [])
        for f in s.get("fields", [])
        if f.get("required") and not f.get("readonly")
        and (f.get("value") and str(f["value"]).strip())
    ), missing


def _clean_label(label: str, fid: str = "") -> str:
    """Strip Marathi text and punctuation, return clean English label."""
    l = re.sub(r"\([^)]*\)", "", label).strip()          # strip (मराठी)
    l = re.sub(r"[\u0900-\u0D7F]+", "", l).strip()       # strip Devanagari
    l = re.sub(r"[*†:]+", "", l).strip()
    if l:
        return l.title() if l.islower() else l
    return fid.replace("_", " ").replace("-", " ").title()


def _list_fields(fields: list[dict], max_n: int = 4) -> str:
    """'Category, Date, Supplier and 2 more'."""
    names = [_clean_label(f.get("label", ""), f.get("field_id", "")) for f in fields[:max_n]]
    extra = len(fields) - max_n
    if extra > 0:
        return ", ".join(names) + f" and {extra} more"
    if len(names) > 1:
        return ", ".join(names[:-1]) + f" and {names[-1]}"
    return names[0] if names else ""


def build_page_announcement(
    context: dict,
    url:     str  = "",
    enabled: bool = True,
) -> str:
    """
    Build a rich, Copilot-like spoken announcement for the current page.

    enabled=True  → user just toggled CoPilot ON
    enabled=False → new page navigation detected, CoPilot already ON
    """
    page        = context.get("page", {})
    title       = page.get("title", "") or ""
    page_url    = url or page.get("page_id", "") or ""
    all_fields  = [f for s in context.get("sections", []) for f in s.get("fields", [])]
    field_ids   = [f.get("field_id", "") for f in all_fields]
    short_name, page_desc = _detect_page_type(page_url, title, field_ids)

    total_fill = sum(
        1 for f in all_fields
        if not f.get("readonly") and not f.get("calculated")
    )
    req_fields = [
        f for f in all_fields
        if f.get("required") and not f.get("readonly")
    ]
    missing_fields = [
        f for f in req_fields
        if not (f.get("value") and str(f["value"]).strip())
    ]
    filled_req = len(req_fields) - len(missing_fields)

    # ── Toggle ON greeting ────────────────────────────────────────────────────
    if enabled:
        greetings = [
            "Hey! CoPilot is active.",
            "CoPilot is on and ready.",
            "I'm here — CoPilot is active.",
        ]
        opener = random.choice(greetings)
    else:
        # Navigation announcement — no greeting, just page info
        opener = ""

    parts: list[str] = []
    if opener:
        parts.append(opener)

    # ── No fillable fields — check for table/list data ──────────────────────
    if total_fill == 0:
        # Try table summary first
        try:
            from voice.table_reader import has_table_data, build_table_page_summary
            if has_table_data(context):
                tbl_summary = build_table_page_summary(context)
                # Detect list page name
                list_name = ""
                for pat, name in _LIST_PAGE_PATTERNS:
                    if pat.search(f"{page_url} {title}"):
                        list_name = name
                        break
                if not list_name:
                    list_name = short_name if short_name != "Page" else "records"

                spoken = f"I can see the {list_name} list. {tbl_summary}"
                spoken += " Say 'how many pending' or 'show me last 5' to hear details."
                parts.append(spoken)
                return " ".join(p for p in parts if p)
        except Exception:
            pass

        buttons = context.get("buttons", [])
        nav_count = sum(1 for b in buttons if b.get("is_nav"))
        if nav_count > 0:
            parts.append(
                f"I can see {page_desc}. "
                f"There are no fillable fields here, but I can see {nav_count} navigation links. "
                "Say 'go to' followed by any section name to navigate."
            )
        else:
            parts.append(f"I can see {page_desc}. No fillable fields on this page right now.")
        return " ".join(p for p in parts if p)

    # ── All required filled ──────────────────────────────────────────────────
    if req_fields and not missing_fields:
        parts.append(
            f"I can see {page_desc}. "
            f"Good news — all {len(req_fields)} required fields are already filled. "
            "You can review and hit submit, or ask me to check anything."
        )
        return " ".join(p for p in parts if p)

    # ── Has missing required fields — most common case ───────────────────────
    if missing_fields:
        missing_str = _list_fields(missing_fields, max_n=3)

        # Specific advice by page type
        if short_name == "Purchase Order":
            hint = (
                "To get started, I'll need the Category, Date, and Supplier at minimum. "
                "Note: HSN code, CGST, and SGST are auto-filled based on the product. "
                "Say 'fill required fields' and I'll walk you through them one by one."
            )
        elif short_name == "Sales Order":
            hint = (
                "I'll need the Customer, Product, and Quantity to record this sale. "
                "Say 'fill required fields' to get started."
            )
        elif short_name == "Supplier":
            hint = (
                "Fill in the supplier name and contact details. "
                "Say 'fill required fields' to go through them."
            )
        elif short_name == "Customer":
            hint = (
                "I'll need the customer name and contact info. "
                "Say 'fill required fields' to begin."
            )
        elif short_name in ("Stock / Inventory", "Form"):
            hint = (
                f"I need values for: {missing_str}. "
                "Say 'fill required fields' or just tell me what to fill."
            )
        else:
            hint = (
                f"Still need: {missing_str}. "
                "Say 'fill required fields' to go through them, or just tell me what to fill."
            )

        parts.append(
            f"I can see {page_desc}. "
            f"There are {total_fill} fields — {len(missing_fields)} required "
            f"{'field is' if len(missing_fields) == 1 else 'fields are'} still empty. "
            f"{hint}"
        )

    # ── Some filled, some not ────────────────────────────────────────────────
    elif filled_req > 0 and missing_fields:
        parts.append(
            f"I can see {page_desc}. "
            f"You've filled {filled_req} of {len(req_fields)} required fields. "
            f"Still missing: {_list_fields(missing_fields)}. "
            "Say 'what's left' to hear them, or 'fill required fields' to continue."
        )
    else:
        parts.append(
            f"I can see {page_desc} with {total_fill} fields. "
            "Tell me what to fill, or say 'fill required fields' to get started."
        )

    return " ".join(p for p in parts if p)


def build_toggle_off_message() -> str:
    """Spoken message when the user turns CoPilot OFF."""
    messages = [
        "CoPilot is paused. Toggle me back on whenever you need help.",
        "I'm stepping back. Switch me on again anytime.",
        "CoPilot off. I'll be here when you need me.",
    ]
    return random.choice(messages)