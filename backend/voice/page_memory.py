"""
page_memory.py — Cross-page memory for GoPilot CoPilot

Stores what the user was doing on a page BEFORE they navigate away,
and surfaces it as a resume prompt when they return or land on a related page.

Architecture
────────────
  Tab session ID  (tab-stable, stored in chrome.storage.local)
    └── PageSnapshot  (what the user was doing on a specific URL)
          ├── url, page_type, short_name
          ├── filled_fields  [(field_id, label, value), ...]
          ├── unfilled_required  [field_id, ...]
          ├── last_instruction   last thing the user said
          ├── was_in_guided_fill bool
          └── timestamp

The tab session ID is generated ONCE per Chrome tab and reused for the
lifetime of that tab across all navigations. It is passed from popup.js
in every /voice/process call as `tab_session_id`.

On every navigation the vision_observer sends the PRE-navigation context
snapshot via /voice/page_memory/save. On landing the proactive announcer
checks /voice/page_memory/resume.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# How long (seconds) a page snapshot stays relevant after leaving a page.
# After this, we assume the user has moved on and don't offer to resume.
SNAPSHOT_TTL_SECONDS = 30 * 60   # 30 minutes


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class FilledField:
    field_id: str
    label:    str
    value:    str


@dataclass
class PageSnapshot:
    """Everything worth remembering about a page the user was working on."""
    tab_session_id:    str
    url:               str
    page_type:         str          # "Purchase Order" | "Sales Order" | ...
    short_name:        str          # same short names as proactive_announcer
    filled_fields:     list[FilledField] = field(default_factory=list)
    unfilled_required: list[str]         = field(default_factory=list)  # field_ids
    unfilled_labels:   list[str]         = field(default_factory=list)  # human labels
    last_instruction:  str               = ""
    was_in_guided_fill: bool             = False
    total_fields:      int               = 0
    timestamp:         float             = field(default_factory=time.monotonic)

    def is_stale(self) -> bool:
        return (time.monotonic() - self.timestamp) > SNAPSHOT_TTL_SECONDS

    def has_meaningful_work(self) -> bool:
        """True if user did real work worth resuming."""
        return len(self.filled_fields) >= 1 or self.was_in_guided_fill


# ── Store ─────────────────────────────────────────────────────────────────────

class PageMemoryStore:
    """
    In-memory store of page snapshots keyed by tab_session_id.

    One snapshot per tab — we only remember the MOST RECENT page the user
    was working on. If they navigate A→B→C, only C is remembered (B was
    intermediate and probably not worth resuming).

    Exception: if B had significant unfilled work, we surface a resume prompt
    on the NEXT form page, regardless of what C was.
    """

    def __init__(self) -> None:
        # tab_session_id → PageSnapshot
        self._snapshots: dict[str, PageSnapshot] = {}
        # tab_session_id → list of last N instructions (for context)
        self._instruction_log: dict[str, list[str]] = {}

    # ── Save ──────────────────────────────────────────────────────────────────

    def save_snapshot(
        self,
        tab_session_id: str,
        context:        dict,
        url:            str,
        last_instruction: str = "",
        was_in_guided_fill: bool = False,
    ) -> PageSnapshot | None:
        """
        Snapshot the current page state before the user navigates away.
        Returns the snapshot if it has meaningful work, else None.
        """
        from voice.proactive_announcer import _detect_page_type

        all_fields = [
            f for s in context.get("sections", [])
            for f in s.get("fields", [])
        ]
        field_ids = [f.get("field_id", "") for f in all_fields]
        page      = context.get("page", {})
        title     = page.get("title", "") or ""

        short_name, _ = _detect_page_type(url, title, field_ids)

        # Collect filled fields (non-readonly, has a real value)
        filled = []
        unfilled_ids    = []
        unfilled_labels = []

        for f in all_fields:
            if f.get("readonly") or f.get("calculated"):
                continue
            val = str(f.get("value") or "").strip()
            lbl = _clean_label(f.get("label", ""), f.get("field_id", ""))
            if val:
                filled.append(FilledField(
                    field_id=f.get("field_id", ""),
                    label=lbl,
                    value=val,
                ))
            elif f.get("required"):
                unfilled_ids.append(f.get("field_id", ""))
                unfilled_labels.append(lbl)

        snap = PageSnapshot(
            tab_session_id    = tab_session_id,
            url               = url,
            page_type         = short_name,
            short_name        = short_name,
            filled_fields     = filled,
            unfilled_required = unfilled_ids,
            unfilled_labels   = unfilled_labels,
            last_instruction  = last_instruction,
            was_in_guided_fill= was_in_guided_fill,
            total_fields      = len([f for f in all_fields
                                    if not f.get("readonly") and not f.get("calculated")]),
        )

        if snap.has_meaningful_work():
            self._snapshots[tab_session_id] = snap
            logger.info(
                "PageMemory: saved snapshot for %s — %d filled, %d unfilled required",
                short_name, len(filled), len(unfilled_ids),
            )
            return snap

        # No meaningful work — clear any old snapshot so we don't surface stale prompts
        self._snapshots.pop(tab_session_id, None)
        return None

    # ── Log instructions ──────────────────────────────────────────────────────

    def log_instruction(self, tab_session_id: str, instruction: str) -> None:
        """Keep a rolling log of the last 10 user instructions for this tab."""
        log = self._instruction_log.setdefault(tab_session_id, [])
        log.append(instruction)
        if len(log) > 10:
            self._instruction_log[tab_session_id] = log[-10:]

    def last_instruction(self, tab_session_id: str) -> str:
        log = self._instruction_log.get(tab_session_id, [])
        return log[-1] if log else ""

    # ── Resume ────────────────────────────────────────────────────────────────

    def get_resume_prompt(
        self,
        tab_session_id: str,
        new_url:        str,
        new_context:    dict,
    ) -> str | None:
        """
        Build a spoken resume prompt for the new page, incorporating memory
        of what the user was doing before.

        Returns a resume string to PREPEND to the normal page announcement,
        or None if there's nothing worth resuming.
        """
        snap = self._snapshots.get(tab_session_id)

        if not snap or snap.is_stale() or not snap.has_meaningful_work():
            return None

        # Don't offer resume if we're on the same page
        if _same_page(snap.url, new_url):
            return None

        n_filled = len(snap.filled_fields)

        # ── Case 1: User was mid-guided-fill ─────────────────────────────────
        if snap.was_in_guided_fill and snap.unfilled_required:
            remaining = len(snap.unfilled_required)
            return (
                f"By the way — you were filling a {snap.short_name} and had "
                f"{remaining} required {'field' if remaining == 1 else 'fields'} "
                f"still to go. Say 'go back' if you want to continue that."
            )

        # ── Case 2: User had partially filled a form ──────────────────────────
        if n_filled >= 1 and snap.unfilled_required:
            n_unfilled = len(snap.unfilled_required)
            filled_summary = _summarise_filled(snap.filled_fields, max_n=2)
            return (
                f"By the way — you were filling a {snap.short_name} "
                f"({filled_summary} filled) with {n_unfilled} required "
                f"{'field' if n_unfilled == 1 else 'fields'} still empty. "
                f"Say 'go back' to continue where you left off."
            )

        # ── Case 3: User had filled everything — didn't submit ────────────────
        if n_filled >= 1 and not snap.unfilled_required:
            return (
                f"Quick note — you had a {snap.short_name} filled out but "
                f"hadn't submitted it yet. Say 'go back' if you want to review it."
            )

        return None

    def clear_snapshot(self, tab_session_id: str) -> None:
        """Call this after a successful submit so we don't offer stale resume."""
        self._snapshots.pop(tab_session_id, None)
        logger.info("PageMemory: cleared snapshot for tab %s", tab_session_id)

    def get_snapshot(self, tab_session_id: str) -> PageSnapshot | None:
        snap = self._snapshots.get(tab_session_id)
        if snap and snap.is_stale():
            self._snapshots.pop(tab_session_id, None)
            return None
        return snap


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_label(label: str, fid: str = "") -> str:
    s = re.sub(r"\([^)]*\)", "", label).strip()
    s = re.sub(r"[\u0900-\u0D7F]+", "", s).strip()
    s = re.sub(r"[*†:]+", "", s).strip()
    return s or fid.replace("_", " ").replace("-", " ").title()


def _same_page(url_a: str, url_b: str) -> bool:
    """
    True if two URLs point to the same logical page.
    For SPA hash routing (#/purchase, #/supplier), the hash IS the page —
    include it in comparison but strip query strings.
    """
    def _normalise(u: str) -> str:
        # Strip query string only — keep hash (SPA routes live there)
        u = re.sub(r"\?[^#]*", "", u)   # remove ?query
        u = u.rstrip("/")
        # Normalise hash: treat missing hash as "#/"
        if "#" not in u:
            u = u + "#/"
        return u.lower()
    return _normalise(url_a) == _normalise(url_b)


def _summarise_filled(fields: list[FilledField], max_n: int = 2) -> str:
    """'Category: Chairs, Date: 2024-01-15' — concise filled field summary."""
    items = [f"{f.label}: {f.value}" for f in fields[:max_n]]
    extra = f" +{len(fields) - max_n} more" if len(fields) > max_n else ""
    return ", ".join(items) + extra


# ── Singleton ─────────────────────────────────────────────────────────────────
page_memory_store = PageMemoryStore()