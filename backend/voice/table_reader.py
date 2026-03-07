"""
table_reader.py — Voice answers for table/list pages

Handles queries like:
  "how many pending orders"        → "You have 5 pending orders out of 12 total."
  "show me the last 3 orders"      → "Last 3: PA001 from ABC Traders ₹45,000 — Pending..."
  "how many completed"             → "6 orders are completed."
  "who are my recent suppliers"    → "Recent suppliers: ABC Traders, XYZ Ltd, Raju Traders."
  "tell me about this page"        → includes table summary in page description

Zero LLM. Reads from context["tables"] populated by extractor.js.
<5ms per query.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ── Query intent patterns ─────────────────────────────────────────────────────

_HOW_MANY_RE  = re.compile(r"\b(how many|count|total|kitne|kaay|kiti|are there any|any.*pending|any.*cancelled)\b", re.I)
_SHOW_RE      = re.compile(r"\b(show|list|give me|tell me|what are|which|who are|dikhao)\b", re.I)
_LAST_N_RE    = re.compile(r"\b(?:last|recent|latest|top)\s+(\d+)\b", re.I)
_STATUS_RE    = re.compile(r"\b(pending|completed|cancelled|canceled|approved|rejected|open|closed|paid|unpaid|draft)\b", re.I)
_SUPPLIER_RE  = re.compile(r"\b(supplier|vendor|party|sapplayr)\b|who.{0,20}supplier", re.I)
_CUSTOMER_RE  = re.compile(r"\b(customer|client|buyer)\b", re.I)
_AMOUNT_RE    = re.compile(r"\b(amount|total|value|money|rupee|rs|₹)\b", re.I)
_DATE_RE      = re.compile(r"\b(date|when|today|recent|latest)\b", re.I)
_TABLE_PAGE_RE= re.compile(
    r"\b(orders?|purchases?|sales|invoices?|list|history|records?|entries|items?)\b", re.I
)


def has_table_data(context: dict) -> bool:
    """True if context has table data worth talking about."""
    tables = context.get("tables", [])
    return bool(tables and any(t.get("row_count", 0) > 0 for t in tables))


def is_table_query(instruction: str, context: dict) -> bool:
    """True if the instruction is asking about table/list data."""
    if not has_table_data(context):
        return False
    instr = instruction.lower()
    return bool(
        _HOW_MANY_RE.search(instr) or
        _SHOW_RE.search(instr) or
        _TABLE_PAGE_RE.search(instr) or
        _STATUS_RE.search(instr)
    )


def answer_table_query(instruction: str, context: dict) -> str | None:
    """
    Answer a voice query about table/list page data.

    Returns a spoken answer string, or None if we can't answer.
    """
    tables = context.get("tables", [])
    if not tables:
        return None

    # Use the largest / most relevant table
    table = _pick_table(tables, instruction)
    if not table:
        return None

    instr = instruction.lower().strip()
    rows  = table.get("rows", [])
    total = table.get("row_count", len(rows))

    # ── "How many X" ─────────────────────────────────────────────────────────
    # Skip "how many" if this is clearly an amount question
    if _HOW_MANY_RE.search(instr) and not _AMOUNT_RE.search(instr):
        status_match = _STATUS_RE.search(instr)
        if status_match:
            status_word = status_match.group(1).lower()
            # Normalise status variants
            status_norm = _normalise_status(status_word)
            count = _count_by_status(table, status_norm)
            if count is not None:
                caption = table.get("caption", "records")
                return (
                    f"You have {count} {status_word} {caption.lower()}. "
                    f"Total: {total}."
                )

        # "How many orders total"
        caption = table.get("caption", "records")
        status_summary = table.get("status_summary", {})
        if status_summary:
            parts = [f"{v} {k.lower()}" for k, v in list(status_summary.items())[:4]]
            return f"You have {total} {caption.lower()} — {', '.join(parts)}."
        return f"You have {total} {caption.lower()} on this page."

    # ── "Who are my recent suppliers / customers" ─────────────────────────────
    if _SUPPLIER_RE.search(instr):
        party_col = _find_column(table, ["supplier", "vendor", "party", "from", "seller"])
        if party_col:
            names = list(dict.fromkeys(
                r.get(party_col, "") for r in rows if r.get(party_col)
            ))[:5]
            if names:
                return f"Recent suppliers: {', '.join(names)}."

    if _CUSTOMER_RE.search(instr):
        party_col = _find_column(table, ["customer", "client", "buyer", "to"])
        if party_col:
            names = list(dict.fromkeys(
                r.get(party_col, "") for r in rows if r.get(party_col)
            ))[:5]
            if names:
                return f"Recent customers: {', '.join(names)}."

    # ── "Show me / list / give me last N" ────────────────────────────────────
    last_n_m = _LAST_N_RE.search(instr)
    n = int(last_n_m.group(1)) if last_n_m else None

    if _SHOW_RE.search(instr) or n:
        status_match = _STATUS_RE.search(instr)
        status_filter = _normalise_status(status_match.group(1)) if status_match else None

        filtered = rows
        if status_filter:
            filtered = [r for r in rows if _row_has_status(r, status_filter)]

        display = filtered[: n or 5]
        if not display:
            status_word = status_match.group(1).lower() if status_match else ""
            return f"I don't see any {status_word} records in the visible rows."

        headers = table.get("headers", [])
        items = [_describe_row(r, headers) for r in display]
        label = f"last {len(display)}" if not status_filter else f"{status_filter.lower()}"
        caption = table.get("caption", "records")
        has_more = len(filtered) > (n or 5)
        suffix = f" and {len(filtered) - (n or 5)} more" if has_more else ""
        return f"{label.title()} {caption.lower()}: {'; '.join(items)}{suffix}."

    # ── "Show me / list / give me last N" ────────────────────────────────────
    last_n_m = _LAST_N_RE.search(instr)
    n = int(last_n_m.group(1)) if last_n_m else None

    if _SHOW_RE.search(instr) or n:
        status_match = _STATUS_RE.search(instr)
        status_filter = _normalise_status(status_match.group(1)) if status_match else None

        filtered = rows
        if status_filter:
            filtered = [r for r in rows if _row_has_status(r, status_filter)]

        display = filtered[: n or 5]
        if not display:
            status_word = status_match.group(1).lower() if status_match else ""
            return f"I don't see any {status_word} records in the visible rows."

        headers = table.get("headers", [])
        items = [_describe_row(r, headers) for r in display]
        label = f"last {len(display)}" if not status_filter else f"{status_filter.lower()}"
        caption = table.get("caption", "records")
        has_more = len(filtered) > (n or 5)
        suffix = f" and {len(filtered) - (n or 5)} more" if has_more else ""
        return f"{label.title()} {caption.lower()}: {'; '.join(items)}{suffix}."

    # ── "Total amount pending / what's the value" ────────────────────────────
    if _AMOUNT_RE.search(instr):
        status_match = _STATUS_RE.search(instr)
        status_filter = _normalise_status(status_match.group(1)) if status_match else None

        amount_col = _find_column(table, ["amount", "total", "value", "price", "cost"])
        if amount_col:
            target_rows = rows
            if status_filter:
                target_rows = [r for r in rows if _row_has_status(r, status_filter)]
            total_amt = _sum_column(target_rows, amount_col)
            if total_amt is not None:
                label = f"{status_filter.lower()} " if status_filter else ""
                return f"Total {label}amount from visible rows: ₹{total_amt:,.0f}."

    return None


def build_table_page_summary(context: dict) -> str:
    """
    Build a concise table-page summary for page announcements.
    Used by proactive_announcer when on a list/table page.
    """
    tables = context.get("tables", [])
    if not tables:
        return ""

    parts = []
    for table in tables[:2]:  # summarise up to 2 tables
        caption = table.get("caption", "records")
        total   = table.get("row_count", 0)
        status  = table.get("status_summary", {})

        if status:
            status_parts = [f"{v} {k.lower()}" for k, v in list(status.items())[:3]]
            parts.append(f"{total} {caption.lower()} — {', '.join(status_parts)}")
        elif total:
            parts.append(f"{total} {caption.lower()}")

    if not parts:
        return ""

    return "I can see " + "; and ".join(parts) + "."


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pick_table(tables: list[dict], instruction: str) -> dict | None:
    """Pick the most relevant table for the query."""
    if not tables:
        return None
    if len(tables) == 1:
        return tables[0]

    instr = instruction.lower()
    # Prefer table whose caption matches the query
    for t in tables:
        caption = (t.get("caption") or "").lower()
        if caption and caption in instr:
            return t

    # Default: largest table
    return max(tables, key=lambda t: t.get("row_count", 0))


def _normalise_status(word: str) -> str:
    """Normalise status words to title case."""
    mapping = {
        "pending":   "Pending",
        "completed": "Completed",
        "complete":  "Completed",
        "cancelled": "Cancelled",
        "canceled":  "Cancelled",
        "approved":  "Approved",
        "rejected":  "Rejected",
        "open":      "Open",
        "closed":    "Closed",
        "paid":      "Paid",
        "unpaid":    "Unpaid",
        "draft":     "Draft",
    }
    return mapping.get(word.lower(), word.title())


def _count_by_status(table: dict, status: str) -> int | None:
    """Count rows with a given status from status_summary or row scan."""
    summary = table.get("status_summary", {})
    if summary:
        # Try exact match first
        for k, v in summary.items():
            if k.lower() == status.lower():
                return v
        # Partial match
        for k, v in summary.items():
            if status.lower() in k.lower() or k.lower() in status.lower():
                return v

    # Fall back to scanning rows
    rows = table.get("rows", [])
    if rows:
        count = sum(1 for r in rows if _row_has_status(r, status))
        return count

    return None


def _row_has_status(row: dict, status: str) -> bool:
    """Check if any cell in the row matches the status."""
    for v in row.values():
        if isinstance(v, str) and v.lower() == status.lower():
            return True
    return False


def _find_column(table: dict, keywords: list[str]) -> str | None:
    """Find a column header that matches any of the keywords."""
    headers = table.get("headers", [])
    for header in headers:
        h_lower = header.lower()
        for kw in keywords:
            if kw in h_lower or h_lower in kw:
                return header
    return None


def _sum_column(rows: list[dict], col: str) -> float | None:
    """Sum numeric values in a column."""
    total = 0.0
    found = False
    for row in rows:
        val = row.get(col, "")
        if val:
            # Strip currency symbols, commas
            clean = re.sub(r"[₹$,\s]", "", str(val))
            try:
                total += float(clean)
                found = True
            except ValueError:
                pass
    return total if found else None


def _describe_row(row: dict, headers: list[str]) -> str:
    """Describe a single row in natural spoken form."""
    # Pick the most informative columns: invoice/order no, party, amount, status
    priority = ["invoice", "order", "no", "number", "id",
                "supplier", "customer", "party",
                "amount", "total", "value",
                "status", "date"]

    selected_vals: list[str] = []
    used_headers: set[str]   = set()
    for kw in priority:
        for h in headers:
            if h in used_headers:
                continue
            if kw in h.lower() and h in row and row[h]:
                val = str(row[h]).strip()
                if val and val not in selected_vals and len(selected_vals) < 3:
                    selected_vals.append(val)
                    used_headers.add(h)
                    break

    if not selected_vals:
        selected_vals = [str(v) for v in list(row.values())[:3] if v]

    return " — ".join(selected_vals)