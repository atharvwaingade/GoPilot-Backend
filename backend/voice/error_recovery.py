"""
error_recovery.py — Validation error classification and fix-offer for GoPilot CoPilot

Three error sources:
  A. executor.js {ok:false, reason:"..."} — field not found, option mismatch, bad date
  B. DOM inline errors after fill       — .invalid-feedback, aria-invalid
  C. Toast/server errors after submit   — duplicate, required field server-side

Copilot-style recovery:
  "No option matching 'Cahirs'. Available: Chairs, Tables, Sofas.
   Did you mean Chairs? Say yes to use that, or tell me the correct option."

  "Date format wasn't accepted.
   Should I try today's date instead? Say yes or give me the date."

  "Supplier is required — what should I enter?"
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ── Error kinds ───────────────────────────────────────────────────────────────

class ErrorKind:
    OPTION_NOT_FOUND  = "option_not_found"
    DATE_INVALID      = "date_invalid"
    FIELD_NOT_FOUND   = "field_not_found"
    FIELD_DISABLED    = "field_disabled"
    REQUIRED_EMPTY    = "required_empty"
    DOM_VALIDATION    = "dom_validation"
    SERVER_VALIDATION = "server_validation"
    GENERIC           = "generic"


@dataclass
class RecoveryAction:
    kind:     str        # "fill" | "ask" | "skip"
    field_id: str = ""
    label:    str = ""
    value:    str = ""   # suggested corrected value (fill kind)
    spoken:   str = ""   # what to say when applying this fix


@dataclass
class ErrorRecovery:
    error_kind:     str
    field_id:       str
    field_label:    str
    original_value: str
    spoken_error:   str          # describes the problem
    spoken_offer:   str          # proposes the fix
    recovery:       RecoveryAction | None
    is_blocking:    bool = False


# ── Executor error patterns ───────────────────────────────────────────────────

_OPTION_RE   = re.compile(r'No option matching ["\']([^"\']+)["\']\.?\s*Options?:\s*(.+)', re.I)
_DATE_RE     = re.compile(r'Cannot parse date', re.I)
_DISABLED_RE = re.compile(r'is disabled', re.I)
_NOT_FOUND_RE= re.compile(r'not found|Field not found', re.I)


def parse_executor_error(
    reason:         str,
    field_id:       str,
    field_label:    str,
    original_value: str,
    screen_context: dict | None = None,
) -> ErrorRecovery:
    """Turn an executor.js {ok:false, reason} into a voiced recovery."""
    label = _clean(field_label, field_id)

    # ── Option mismatch ───────────────────────────────────────────────────────
    m = _OPTION_RE.search(reason)
    if m:
        tried   = m.group(1).strip()
        options = [o.strip().strip('"\'') for o in m.group(2).split(",")][:8]
        best    = _closest(tried, options)
        opt_str = ", ".join(options[:5])
        extra   = f" and {len(options)-5} more" if len(options) > 5 else ""

        spoken_error = (
            f"I couldn't match '{tried}' to any option for {label}. "
            f"Available: {opt_str}{extra}."
        )
        if best:
            spoken_offer = f"Did you mean '{best}'? Say yes to use that, or say the correct option."
            recovery = RecoveryAction("fill", field_id, label, best,
                                      f"Setting {label} to {best}.")
        else:
            spoken_offer = f"Which option would you like for {label}?"
            recovery = RecoveryAction("ask", field_id, label)

        return ErrorRecovery(ErrorKind.OPTION_NOT_FOUND, field_id, label,
                             tried, spoken_error, spoken_offer, recovery)

    # ── Date parse failure ────────────────────────────────────────────────────
    if _DATE_RE.search(reason):
        from datetime import date
        today = date.today().strftime("%d/%m/%Y")
        return ErrorRecovery(
            ErrorKind.DATE_INVALID, field_id, label, original_value,
            spoken_error=f"'{original_value}' wasn't recognised as a date for {label}.",
            spoken_offer=f"Should I use today's date, {today}? Say yes, or give me the date.",
            recovery=RecoveryAction("fill", field_id, label, today,
                                    f"Setting {label} to today, {today}."),
        )

    # ── Field disabled ────────────────────────────────────────────────────────
    if _DISABLED_RE.search(reason):
        return ErrorRecovery(
            ErrorKind.FIELD_DISABLED, field_id, label, original_value,
            spoken_error=f"{label} is disabled and can't be edited right now.",
            spoken_offer="It may be calculated automatically or controlled by another field.",
            recovery=None,
        )

    # ── Field not found ───────────────────────────────────────────────────────
    if _NOT_FOUND_RE.search(reason):
        suggestion = _find_similar_field(field_id, screen_context)
        if suggestion:
            return ErrorRecovery(
                ErrorKind.FIELD_NOT_FOUND, field_id, label, original_value,
                spoken_error=f"I couldn't find a '{label}' field on this page.",
                spoken_offer=f"Did you mean '{suggestion}'? Say yes to fill that instead.",
                recovery=RecoveryAction("ask", field_id, label),
            )
        return ErrorRecovery(
            ErrorKind.FIELD_NOT_FOUND, field_id, label, original_value,
            spoken_error=f"I couldn't find '{label}' on this page.",
            spoken_offer="Say 'list fields' to hear what's available.",
            recovery=None,
        )

    # ── Generic ───────────────────────────────────────────────────────────────
    return ErrorRecovery(
        ErrorKind.GENERIC, field_id, label, original_value,
        spoken_error=f"There was a problem filling {label}: {reason[:80]}.",
        spoken_offer=f"Want to try a different value for {label}? Just say what to use.",
        recovery=RecoveryAction("ask", field_id, label),
    )


def parse_dom_error(
    field_id:       str,
    field_label:    str,
    filled_value:   str,
    dom_error_text: str,
) -> ErrorRecovery | None:
    """Turn a DOM inline validation message into a recovery."""
    if not dom_error_text or len(dom_error_text.strip()) < 3:
        return None
    label = _clean(field_label, field_id)
    err   = dom_error_text.strip().rstrip(".")

    if re.search(r'\b(required|mandatory|cannot be empty|please enter|must)\b', err, re.I):
        return ErrorRecovery(
            ErrorKind.REQUIRED_EMPTY, field_id, label, filled_value,
            spoken_error=f"{label} is required — {err.lower()}.",
            spoken_offer=f"What should I enter for {label}?",
            recovery=RecoveryAction("ask", field_id, label),
            is_blocking=True,
        )

    if re.search(r'\b(too long|maximum|max.*char|exceed)\b', err, re.I):
        return ErrorRecovery(
            ErrorKind.DOM_VALIDATION, field_id, label, filled_value,
            spoken_error=f"The value for {label} is too long. {err}.",
            spoken_offer=f"Please give me a shorter value for {label}.",
            recovery=RecoveryAction("ask", field_id, label),
        )

    return ErrorRecovery(
        ErrorKind.DOM_VALIDATION, field_id, label, filled_value,
        spoken_error=f"Validation issue with {label}: {err}.",
        spoken_offer=f"Want to try a different value? Just tell me what to use for {label}.",
        recovery=RecoveryAction("ask", field_id, label),
    )


def parse_submit_error(
    toast_text:     str,
    screen_context: dict | None = None,
) -> ErrorRecovery | None:
    """Turn a server-side toast error into a recovery."""
    if not toast_text:
        return None
    text = toast_text.strip()
    if not re.search(r'\b(error|failed|invalid|required|cannot|duplicate|please|must)\b', text, re.I):
        return None

    matched = _field_from_error(text, screen_context)
    if matched:
        fid   = matched.get("field_id", "")
        label = _clean(matched.get("label", ""), fid)
        return ErrorRecovery(
            ErrorKind.SERVER_VALIDATION, fid, label,
            matched.get("value", "") or "",
            spoken_error=f"Submit failed: {text[:100]}.",
            spoken_offer=f"The problem looks like it's with {label}. What should I change it to?",
            recovery=RecoveryAction("ask", fid, label),
            is_blocking=True,
        )

    return ErrorRecovery(
        ErrorKind.SERVER_VALIDATION, "", "", "",
        spoken_error=f"Submit failed: {text[:100]}.",
        spoken_offer="Please fix the issue and say submit again.",
        recovery=None,
        is_blocking=True,
    )


# ── Recovery state machine ────────────────────────────────────────────────────

class RecoveryState:
    """
    Per-session pending recovery tracker.

    When a fill fails, the controller calls set_pending().
    On the NEXT voice input, is_in_recovery() returns True and
    build_retry_action() processes the user's response:
      "yes"         → apply the suggested fix value
      "no"          → ask for a different value
      any other     → treat as the corrected value directly
      "cancel/skip" → abandon recovery
    """

    def __init__(self) -> None:
        self._pending: dict[str, ErrorRecovery] = {}

    def set_pending(self, session_id: str, rec: ErrorRecovery) -> None:
        self._pending[session_id] = rec
        logger.info("Recovery pending: session=%s field=%s kind=%s",
                    session_id, rec.field_id, rec.error_kind)

    def is_in_recovery(self, session_id: str) -> bool:
        return session_id in self._pending

    def get_pending(self, session_id: str) -> ErrorRecovery | None:
        return self._pending.get(session_id)

    def clear(self, session_id: str) -> None:
        self._pending.pop(session_id, None)

    # intent helpers
    @staticmethod
    def _yes(text: str) -> bool:
        return bool(re.search(
            r'^\s*(yes|yep|yeah|ok|okay|sure|correct|haan|ha|theek|use that|go ahead|proceed)\s*$',
            text, re.I))

    @staticmethod
    def _no(text: str) -> bool:
        return bool(re.search(
            r'^\s*(no|nope|nahi|nahin|different|other|something else)\s*$',
            text, re.I))

    @staticmethod
    def _cancel(text: str) -> bool:
        return bool(re.search(r'\b(cancel|stop|skip|never mind|abort)\b', text, re.I))

    def build_retry_action(
        self,
        session_id:    str,
        user_response: str,
    ) -> tuple[dict | None, str]:
        """
        Process the user's spoken response to a recovery offer.
        Returns (action_dict_or_None, spoken_text).
        """
        rec = self._pending.get(session_id)
        if not rec:
            return None, "I'm not waiting for a correction right now."

        text = user_response.strip()

        # ── Cancel / skip ─────────────────────────────────────────────────────
        if self._cancel(text):
            self.clear(session_id)
            return None, f"Okay, skipping {rec.field_label}. What would you like to do next?"

        # ── Yes → use suggested value ─────────────────────────────────────────
        if self._yes(text) and rec.recovery and rec.recovery.value:
            self.clear(session_id)
            action = {
                "action":   "tool_call",
                "field_id": rec.field_id,
                "label":    rec.field_label,
                "value":    rec.recovery.value,
                "type":     "text",
                "reason":   "error_recovery",
            }
            return action, rec.recovery.spoken or f"Retrying with '{rec.recovery.value}'."

        # ── No → prompt for different value ──────────────────────────────────
        if self._no(text):
            return None, f"What should I use for {rec.field_label}?"

        # ── User gave a new value directly ────────────────────────────────────
        if len(text) > 0:
            # If user issued a DIFFERENT field command, escape recovery entirely
            # e.g. "set unit to PCS" during supplier recovery → clear and re-route
            _FILL_CMD = re.compile(
                r"^(fill|set|enter|select|put|change)\s+(the\s+)?(\w[\w\s]{1,30}?)\s+(to|as|=)\s+(.+)$",
                re.I
            )
            if _FILL_CMD.match(text):
                self.clear(session_id)
                return None, ""   # empty spoken = voice_controller re-routes normally

            self.clear(session_id)
            clean_val = text.rstrip(".,!? ")
            action = {
                "action":   "tool_call",
                "field_id": rec.field_id,
                "label":    rec.field_label,
                "value":    clean_val,
                "type":     "text",
                "reason":   "error_recovery",
            }
            return action, f"Got it — trying {rec.field_label} as {clean_val}."

        return None, f"What should {rec.field_label} be?"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean(label: str, fid: str = "") -> str:
    s = re.sub(r"\([^)]*\)", "", label).strip()
    s = re.sub(r"[\u0900-\u0D7F]+", "", s).strip()
    s = re.sub(r"[*†:]+", "", s).strip()
    return s or fid.replace("_", " ").replace("-", " ").title()


def _closest(tried: str, options: list[str]) -> str | None:
    """Closest option by character overlap (>50% shared chars)."""
    t = tried.lower().strip()
    t_chars = set(t)
    best_score, best_opt = 0.0, None
    for opt in options:
        o = opt.lower().strip()
        if t in o or o in t:
            return opt
        score = len(t_chars & set(o)) / max(len(t_chars), len(set(o)), 1)
        if score > best_score and score > 0.5:
            best_score, best_opt = score, opt
    return best_opt


def _find_similar_field(field_id: str, context: dict | None) -> str | None:
    if not context:
        return None
    needle = field_id.lower().replace("_", " ")
    for s in context.get("sections", []):
        for f in s.get("fields", []):
            lbl = _clean(f.get("label", ""), f.get("field_id", "")).lower()
            fid = f.get("field_id", "").lower().replace("_", " ")
            if needle in lbl or lbl in needle or needle in fid:
                return _clean(f.get("label", ""), f.get("field_id", ""))
    return None


def _field_from_error(error_text: str, context: dict | None) -> dict | None:
    if not context:
        return None
    err = error_text.lower()
    for s in context.get("sections", []):
        for f in s.get("fields", []):
            lbl = _clean(f.get("label", ""), f.get("field_id", "")).lower()
            fid = f.get("field_id", "").lower()
            if (lbl and lbl in err) or (fid and fid in err):
                return f
    return None


def build_recovery_spoken(rec: ErrorRecovery) -> str:
    """Combine error + offer into one natural sentence."""
    return f"{rec.spoken_error} {rec.spoken_offer}".strip()


# ── Singleton ─────────────────────────────────────────────────────────────────
recovery_state = RecoveryState()