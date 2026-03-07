"""
visual_vad.py — Visual Voice Activity Detection

The direct analogue of audio VAD in Copilot Voice.
Instead of detecting "is someone speaking?", this detects "has the screen
changed in a way that requires reasoning?"

Three signal types ranked by cost:
  1. Structural DOM change  (cheapest — MutationObserver-reported diff)
  2. URL / page navigation  (free — sent from content script)
  3. Value changes on required fields (highest priority)

Threshold strategy:
  - Required field value changes: always interesting
  - New sections / fields appearing: always interesting  
  - Navigation (URL change): always interesting
  - Button state changes: interesting if submit/confirm
  - Cosmetic re-renders: ignored (same field_ids, same values)
"""
from __future__ import annotations

import hashlib
import re
import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ChangeType(str, Enum):
    NAVIGATION      = "navigation"
    FIELD_ADDED     = "field_added"
    FIELD_REMOVED   = "field_removed"
    VALUE_CHANGED   = "value_changed"
    SECTION_CHANGED = "section_changed"
    BUTTON_STATE    = "button_state"
    NO_CHANGE       = "no_change"


@dataclass
class VADResult:
    interesting: bool
    change_type: ChangeType
    changed_fields: list[str]
    added_fields: list[str]
    removed_fields: list[str]
    url_changed: bool
    salience_score: float
    reason: str


def _context_hash(context: dict) -> str:
    try:
        fields = [
            {"id": f.get("field_id"), "value": f.get("value")}
            for section in context.get("sections", [])
            for f in section.get("fields", [])
        ]
        payload = json.dumps(
            {"url": context.get("page", {}).get("page_id"), "fields": fields},
            sort_keys=True, default=str,
        )
    except Exception:
        payload = str(context)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def _field_map(context: dict) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for section in context.get("sections", []):
        for f in section.get("fields", []):
            fid = f.get("field_id")
            if fid:
                result[fid] = f.get("value")
    return result


class VisualVAD:
    """
    Compares two consecutive screen context snapshots and decides
    whether the change is semantically interesting enough to trigger
    a full LLM reasoning cycle.

    Drop-stale policy: if the pipeline is busy when a new event arrives,
    the OLD pending snapshot is dropped — newest frame always wins.
    """

    THRESHOLD     = 0.30
    W_NAVIGATE    = 1.00
    W_FIELD_ADD   = 0.70
    W_FIELD_REM   = 0.60
    W_VALUE_REQ   = 0.80
    W_VALUE_OPT   = 0.40
    W_BUTTON      = 0.25
    W_SECTION     = 0.50

    # BUG 3 FIX: Pages that should never trigger autonomous reasoning.
    # Login/auth pages fire constant value_changed events as the user types
    # credentials, causing a storm of TTS + LLM cycles. We suppress VAD for
    # these pages entirely (navigation events still pass through).
    _SUPPRESS_URL_PATTERNS = re.compile(
        r"login|signin|sign[-_]in|logout|auth|oauth|sso|password|forgot|reset",
        re.IGNORECASE,
    )

    def _is_suppressed_page(self, url: str | None) -> bool:
        """Return True for login/auth pages that should not trigger auto-reasoning."""
        if not url:
            return False
        return bool(self._SUPPRESS_URL_PATTERNS.search(url))

    def compare(
        self,
        prev_context: dict | None,
        curr_context: dict,
        prev_url: str | None = None,
        curr_url: str | None = None,
    ) -> VADResult:
        if prev_context is None:
            return VADResult(True, ChangeType.NAVIGATION, [], [], [], True, 1.0,
                             "First frame — initial page context established")

        if _context_hash(prev_context) == _context_hash(curr_context):
            return VADResult(False, ChangeType.NO_CHANGE, [], [], [], False, 0.0,
                             "Context hash identical")

        url_changed = bool(curr_url and prev_url and curr_url != prev_url)
        if url_changed:
            return VADResult(True, ChangeType.NAVIGATION, [], [], [], True,
                             self.W_NAVIGATE, f"Navigation: {prev_url} → {curr_url}")

        # BUG 3 FIX: Suppress non-navigation VAD events on login/auth pages.
        # This stops the keystroke-spam loop where every character typed in the
        # username/password field fires a VAD event, triggering TTS + LLM.
        if self._is_suppressed_page(curr_url):
            return VADResult(False, ChangeType.NO_CHANGE, [], [], [], False, 0.0,
                             f"Suppressed — auth/login page: {curr_url}")

        prev_fields = _field_map(prev_context)
        curr_fields = _field_map(curr_context)
        prev_ids    = set(prev_fields)
        curr_ids    = set(curr_fields)

        added   = list(curr_ids - prev_ids)
        removed = list(prev_ids - curr_ids)
        changed = [fid for fid in (curr_ids & prev_ids)
                   if curr_fields[fid] != prev_fields[fid]]

        required_ids: set[str] = {
            f.get("field_id", "")
            for section in curr_context.get("sections", [])
            for f in section.get("fields", [])
            if f.get("required")
        }

        score   = 0.0
        reasons = []

        if added:
            score += self.W_FIELD_ADD * min(len(added), 3) / 3
            reasons.append(f"Fields added: {added[:3]}")
        if removed:
            score += self.W_FIELD_REM * min(len(removed), 3) / 3
            reasons.append(f"Fields removed: {removed[:3]}")
        for fid in changed:
            score += (self.W_VALUE_REQ if fid in required_ids else self.W_VALUE_OPT) \
                     / max(len(changed), 1)
        if changed:
            reasons.append(f"Values changed: {changed[:4]}")

        prev_sids = {s.get("section_id") for s in prev_context.get("sections", [])}
        curr_sids = {s.get("section_id") for s in curr_context.get("sections", [])}
        if prev_sids != curr_sids:
            score += self.W_SECTION
            reasons.append("Section structure changed")

        prev_btns = {b.get("button_id"): b.get("disabled")
                     for b in prev_context.get("buttons", [])}
        curr_btns = {b.get("button_id"): b.get("disabled")
                     for b in curr_context.get("buttons", [])}
        if prev_btns != curr_btns:
            score += self.W_BUTTON
            reasons.append("Button state changed")

        score = min(score, 1.0)

        if added:         ct = ChangeType.FIELD_ADDED
        elif removed:     ct = ChangeType.FIELD_REMOVED
        elif changed:     ct = ChangeType.VALUE_CHANGED
        elif prev_sids != curr_sids: ct = ChangeType.SECTION_CHANGED
        else:             ct = ChangeType.BUTTON_STATE

        return VADResult(
            interesting=score >= self.THRESHOLD,
            change_type=ct,
            changed_fields=changed,
            added_fields=added,
            removed_fields=removed,
            url_changed=url_changed,
            salience_score=round(score, 3),
            reason=" | ".join(reasons) if reasons else "Minor change below threshold",
        )


visual_vad = VisualVAD()