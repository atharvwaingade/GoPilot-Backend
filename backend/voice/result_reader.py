"""
result_reader.py — Post-action spoken readback builder

Three responsibilities:
  1. fill_readback()   — "Done — Category is set to Chairs."
  2. submit_readback() — interprets toast/alert text into natural speech
  3. nav_readback()    — "Opened Purchase Order. 8 fields, 3 still empty."

Called by:
  - popup.js  (client-side, via result_scanner.js)  ← primary path
  - event_loop (when VAD detects value_changed after a fill) ← secondary

The primary path is entirely client-side (result_scanner.js injected into tab).
This module provides the BACKEND equivalents for cases where the vision loop
needs to produce readback from screen_context diffs.
"""
from __future__ import annotations

import logging
import re
import random
from typing import Any

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean(label: str, fid: str = "") -> str:
    """Strip Devanagari / Marathi from a field label."""
    s = re.sub(r"\([^)]*\)", "", label).strip()
    s = re.sub(r"[\u0900-\u0D7F]+", "", s).strip()
    s = re.sub(r"[*†:]+", "", s).strip()
    return s or fid.replace("_", " ").title()


def _opening() -> str:
    return random.choice(["Done —", "Got it —", "Set —", "Filled —"])


# ── Fill readback ─────────────────────────────────────────────────────────────

def fill_readback(
    action: dict,
    screen_context: dict | None,
    actual_value: str | None = None,
) -> str:
    """
    Build a spoken confirmation after a field was filled.

    Args:
        action:         The tool_call action dict (field_id, label, value, type).
        screen_context: Current screen context (used to find next empty required field).
        actual_value:   The value actually in the DOM after fill (from result_scanner.js).
                        If None, falls back to action["value"].
    """
    fid   = action.get("field_id", "")
    label = _clean(action.get("label", ""), fid)
    value = actual_value or str(action.get("value", "") or "")
    ftype = action.get("type", "text")

    # Natural value rendering
    if ftype == "checkbox":
        hv = "checked" if str(value).lower() in ("true","1","on","yes") else "unchecked"
    elif ftype in ("select", "combobox") and value:
        hv = value   # already human-readable from DOM readback
    elif ftype == "date":
        hv = f"the date {value}"
    else:
        hv = value or "the value"

    spoken = f"{_opening()} {label} is set to {hv}."

    # Proactive next-field hint
    if screen_context:
        nxt = _next_missing_required(screen_context, skip_fid=fid)
        if nxt:
            nxt_label = _clean(nxt.get("label",""), nxt.get("field_id",""))
            spoken += f" What should I fill for {nxt_label}?"
        else:
            spoken += " Is there anything else you'd like me to fill?"

    return spoken


# ── Submit readback ───────────────────────────────────────────────────────────

# Patterns that indicate a successful submit response
_SUCCESS_RE = re.compile(
    r"\b(success|created|saved|added|submitted|done|complete|"
    r"recorded|updated|invoice|order\s+no|generated|challan)\b",
    re.I,
)
_ERROR_RE = re.compile(
    r"\b(error|failed|invalid|required|cannot|could\s+not|"
    r"duplicate|already\s+exists|warning|please|must)\b",
    re.I,
)
# Reference number patterns (invoice IDs, order numbers)
_REF_RE = re.compile(r"\b([A-Z]{2,}\d{4,}|\d{6,})\b")


def submit_readback(toast_text: str | None, form_cleared: bool = False) -> str:
    """
    Build a spoken response after a form submit.

    Args:
        toast_text:    Text from DOM toast/alert message (may be None).
        form_cleared:  True if form fields were reset after submit (success indicator).
    """
    if toast_text:
        toast_text = toast_text.strip()[:200]
        if _SUCCESS_RE.search(toast_text):
            ref = _REF_RE.search(toast_text)
            ref_str = f" Reference number: {ref.group(1)}." if ref else ""
            return (
                f"Submitted successfully!{ref_str} "
                + _trim_toast(toast_text)
            )
        if _ERROR_RE.search(toast_text):
            return (
                f"There's a problem: {_trim_toast(toast_text)} "
                "Please fix that and try again."
            )
        return _trim_toast(toast_text)

    if form_cleared:
        return (
            "Submitted! The form has been cleared, which means it went through. "
            "You can start a new entry."
        )

    return (
        "Submit was sent. I didn't catch a confirmation message — "
        "check the page to make sure it went through."
    )


def _trim_toast(text: str) -> str:
    """Make toast text voice-friendly."""
    # Remove HTML tags if any slipped through
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Capitalise first letter
    return text[:1].upper() + text[1:] if text else text


# ── Navigation readback ───────────────────────────────────────────────────────

def nav_readback(
    context: dict,
    target_label: str = "",
    url: str = "",
) -> str:
    """
    Build a spoken confirmation after navigation completed.

    Args:
        context:      Screen context of the NEW page.
        target_label: The nav link label that was clicked.
        url:          The new URL.
    """
    from voice.proactive_announcer import build_page_announcement
    try:
        spoken = build_page_announcement(context=context, url=url, enabled=False)
        return spoken
    except Exception:
        pass

    # Minimal fallback
    page = context.get("page", {})
    title = re.sub(r"[-|–].*$", "", page.get("title", "")).strip()
    all_fields = [
        f for s in context.get("sections", []) for f in s.get("fields", [])
        if not f.get("readonly")
    ]
    if not all_fields:
        return f"Opened {title or target_label}. No fillable fields here."

    return f"Opened {title or target_label}. {len(all_fields)} fields available."


# ── Helpers ───────────────────────────────────────────────────────────────────

def _next_missing_required(context: dict, skip_fid: str = "") -> dict | None:
    """Return the next empty required field after the one just filled."""
    for section in context.get("sections", []):
        for f in section.get("fields", []):
            if f.get("field_id") == skip_fid:
                continue
            if f.get("required") and not f.get("readonly"):
                if not (f.get("value") and str(f["value"]).strip()):
                    return f
    return None