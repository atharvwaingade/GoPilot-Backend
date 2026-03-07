"""
guided_fill.py — Guided multi-step form filling for GoPilot CoPilot

When user says "fill required fields" / "fill the form" / "guide me":
  1. Smart field selection — required first, then all empty, then all fillable
  2. Asks for each field by voice, one at a time
  3. Fills and confirms each, moves to next automatically
  4. Handles "skip", "cancel", "go back" mid-flow
  5. Ends with submit prompt

Works on ANY page — even those with no required fields (e.g. item details forms).

State machine per session:
  IDLE → ASKING  (trigger phrase detected)
  ASKING → ASKING  (answer → fill → ask next)
  ASKING → DONE    (all fields answered)
  ASKING → IDLE    (user says cancel/stop)
"""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ── State ─────────────────────────────────────────────────────────────────────

class GuidedFillState(str, Enum):
    IDLE   = "idle"
    ASKING = "asking"
    DONE   = "done"


@dataclass
class GuidedFillSession:
    state:          GuidedFillState = GuidedFillState.IDLE
    pending_fields: list[dict]      = field(default_factory=list)
    current_field:  dict | None     = None
    completed:      list[dict]      = field(default_factory=list)
    skipped:        list[dict]      = field(default_factory=list)
    workflow:       str             = "free"
    auto_submit:    bool            = False
    total_fields:   int             = 0   # total at session start (for progress)
    mode:           str             = "required"  # "required" | "all" | "empty"


# ── Intent patterns ───────────────────────────────────────────────────────────

_GUIDED_TRIGGERS = re.compile(
    r"\b("
    r"fill\s+(all|required|the\s+form|remaining|empty|everything)|"
    r"complete\s+(the\s+)?form|"
    r"fill\s+required|"
    r"guide\s+me|walk\s+me\s+through|"
    r"start\s+fill(ing)?|"
    r"fill\s+everything|"
    r"let'?s?\s+(fill|start)|"
    r"auto\s*fill|"
    r"form\s+fill"
    r")\b",
    re.IGNORECASE,
)

_CANCEL_TRIGGERS = re.compile(
    r"\b(stop|cancel|quit|exit|never\s+mind|abort|pause\s+fill)\b",
    re.IGNORECASE,
)

_SKIP_TRIGGERS = re.compile(
    r"\b(skip|next|leave\s+(it|this)|ignore|pass|don'?t\s+(fill|know)|"
    r"skip\s+this|move\s+on|no\s+value)\b",
    re.IGNORECASE,
)

_DONE_TRIGGERS = re.compile(
    r"\b(done|finish|that'?s\s+(all|it)|complete|enough|stop\s+here)\b",
    re.IGNORECASE,
)


# ── Field selection ───────────────────────────────────────────────────────────

def _select_fields(screen_context: dict, mode: str = "smart") -> tuple[list[dict], str]:
    """
    Select which fields to fill in guided mode.

    Smart priority (mode="smart" or mode="required"):
      1. Required + empty → if any exist
      2. All empty fillable → if no required fields on page
      3. All fillable → if everything is already filled (user wants to re-fill)

    Returns (fields_to_fill, mode_used)
    """
    all_sections = screen_context.get("sections", [])
    all_fields = [
        f for s in all_sections for f in s.get("fields", [])
        if not f.get("readonly") and not f.get("calculated")
    ]

    required_empty = [
        f for f in all_fields
        if f.get("required") and not (f.get("value") and str(f["value"]).strip())
    ]
    all_empty = [
        f for f in all_fields
        if not (f.get("value") and str(f["value"]).strip())
    ]

    # Smart = required-first: if any required empty, fill those
    # Falls through to all-empty only when no required fields on page at all
    if mode in ("smart", "required"):
        if required_empty:
            return required_empty, "required"
        # No required fields → fall through to all empty
    if mode == "all_empty" or (mode in ("smart","required") and not required_empty):
        if all_empty:
            return all_empty, "empty"
    if all_fields:
        return all_fields, "all"   # re-fill mode
    return [], "none"


def _human_label(f: dict) -> str:
    """Clean human-readable label from a field dict."""
    label = f.get("label", "")
    fid   = f.get("field_id", "")
    label = re.sub(r"\([^)]*\)", "", label).strip()
    label = re.sub(r"[\u0900-\u0D7F]+", "", label).strip()
    label = re.sub(r"[*†:]+", "", label).strip()
    if label:
        return label.title() if label.islower() else label
    return fid.replace("_", " ").replace("-", " ").title()


def _ask_for_field(f: dict, position: int, total: int, is_first: bool = False) -> str:
    """Build a natural spoken prompt for a single field."""
    name   = _human_label(f)
    ftype  = f.get("type", "text")
    opts   = f.get("options", [])
    ph     = f.get("placeholder", "") or ""
    req    = f.get("required", False)

    # Progress indicator
    progress = f"({position} of {total}) " if total > 1 else ""

    # Field-type specific prompts
    if ftype == "select" and opts:
        opt_labels = [o.get("label", o.get("value", "")) for o in opts[:6]]
        opt_str    = ", ".join(opt_labels)
        extra      = f" and {len(opts)-6} more" if len(opts) > 6 else ""
        return f"{progress}For {name} — which option? Choices are: {opt_str}{extra}."

    if ftype == "date":
        return f"{progress}What date for {name}? Say today, or give me the date."

    if ftype in ("checkbox", "radio"):
        return f"{progress}Should {name} be yes or no?"

    if ftype == "number":
        unit = re.search(r"\(([^)]+)\)", f.get("label","") or "")
        unit_str = f" in {unit.group(1)}" if unit else ""
        return f"{progress}What number for {name}{unit_str}?"

    # Placeholder as hint
    hint = f' For example: "{ph}".' if ph and len(ph) < 30 else ""
    req_tag = "" if req else " (optional — say skip if you want)"

    openers = [
        f"{progress}What should I enter for {name}{req_tag}?{hint}",
        f"{progress}{name} — what value?{req_tag}{hint}",
        f"{progress}Tell me the {name}{req_tag}.{hint}",
    ]
    return random.choice(openers) if not is_first else openers[0]


# ── Manager ───────────────────────────────────────────────────────────────────

class GuidedFillManager:
    """
    Manages guided fill sessions across multiple concurrent voice sessions.

    Flow:
      voice_controller.process() calls:
        1. is_guided_trigger()      → start_session()
        2. is_in_guided_session()   → handle_answer()
    """

    def __init__(self) -> None:
        self._sessions: dict[str, GuidedFillSession] = {}

    # ── Checks ────────────────────────────────────────────────────────────

    def is_guided_trigger(self, instruction: str) -> bool:
        return bool(_GUIDED_TRIGGERS.search(instruction))

    def is_cancel(self, instruction: str) -> bool:
        return bool(_CANCEL_TRIGGERS.search(instruction))

    def is_skip(self, instruction: str) -> bool:
        return bool(_SKIP_TRIGGERS.search(instruction))

    def is_done(self, instruction: str) -> bool:
        return bool(_DONE_TRIGGERS.search(instruction))

    def is_in_guided_session(self, session_id: str) -> bool:
        s = self._sessions.get(session_id)
        return s is not None and s.state == GuidedFillState.ASKING

    # ── Start ─────────────────────────────────────────────────────────────

    def start_session(
        self,
        session_id:     str,
        screen_context: dict,
        workflow:       str  = "free",
        auto_submit:    bool = False,
        force_mode:     str  = "smart",
    ) -> str:
        """
        Start guided fill. Returns the first spoken prompt.
        Works on any page — with or without required fields.
        """
        fields, mode = _select_fields(screen_context, force_mode)

        if not fields:
            return (
                "I don't see any fillable fields on this page right now. "
                "Navigate to a form and try again."
            )

        session = GuidedFillSession(
            state          = GuidedFillState.ASKING,
            pending_fields = fields[1:],
            current_field  = fields[0],
            completed      = [],
            skipped        = [],
            workflow       = workflow,
            auto_submit    = auto_submit,
            total_fields   = len(fields),
            mode           = mode,
        )
        self._sessions[session_id] = session

        n = len(fields)

        # Opening announcement tailored to mode
        if mode == "required":
            opener = (
                f"Sure — I'll walk you through the {n} required "
                f"{'field' if n==1 else 'fields'}. "
                "Say skip at any point to move on, or cancel to stop. "
            )
        elif mode == "empty":
            opener = (
                f"No required fields marked, so I'll go through all {n} empty "
                f"{'field' if n==1 else 'fields'}. "
                "Say skip to skip one, or cancel to stop. "
            )
        else:  # re-fill
            opener = (
                f"I'll walk you through all {n} "
                f"{'field' if n==1 else 'fields'} on this form. "
                "Say skip to skip one, or cancel to stop. "
            )

        first_prompt = _ask_for_field(fields[0], 1, n, is_first=True)
        logger.info("Guided fill started — session:%s fields:%d mode:%s", session_id, n, mode)
        return opener + first_prompt

    # ── Answer ────────────────────────────────────────────────────────────

    def handle_answer(
        self,
        session_id:    str,
        transcription: str,
    ) -> tuple[dict | None, str]:
        """
        Handle one spoken answer in an active guided fill session.

        Returns (action_dict, spoken_response).
        action_dict is a tool_call (fill field) or None (cancelled/done).
        """
        session = self._sessions.get(session_id)
        if not session or session.state != GuidedFillState.ASKING:
            return None, "I'm not in a guided fill session right now."

        # ── Cancel ────────────────────────────────────────────────────────
        if self.is_cancel(transcription):
            n_done = len(session.completed)
            self._end_session(session_id)
            if n_done > 0:
                return None, (
                    f"Stopped. I filled {n_done} "
                    f"{'field' if n_done==1 else 'fields'} so far. "
                    "You can continue manually or say 'fill the form' to restart."
                )
            return None, "Okay, cancelled. I haven't changed anything."

        # ── Early done ───────────────────────────────────────────────────
        if self.is_done(transcription) and not self.is_skip(transcription):
            n_done = len(session.completed)
            self._end_session(session_id)
            if n_done > 0:
                remaining = 1 + len(session.pending_fields)
                return None, (
                    f"Got it — stopping here. Filled {n_done} "
                    f"{'field' if n_done==1 else 'fields'}, "
                    f"{remaining} still to go. "
                    "Say 'fill the form' to continue later."
                )
            return None, "Stopped without filling anything."

        current = session.current_field
        if not current:
            self._end_session(session_id)
            return None, "All done!"

        fid   = current.get("field_id", "")
        label = _human_label(current)
        done_idx = len(session.completed) + 1          # this field's number
        total    = session.total_fields

        # ── Skip ──────────────────────────────────────────────────────────
        if self.is_skip(transcription):
            session.skipped.append(current)
            logger.info("Guided fill: skipped %s", fid)

            if session.pending_fields:
                nxt            = session.pending_fields[0]
                session.current_field  = nxt
                session.pending_fields = session.pending_fields[1:]
                next_prompt    = _ask_for_field(nxt, done_idx + 1, total)
                spoken = f"Skipped {label}. {next_prompt}"
            else:
                spoken = self._build_completion(session, last_label=None, last_value=None)
                session.state = GuidedFillState.DONE
                self._sessions[session_id] = session

            # No fill action for skipped field
            return {"action": "skip", "field_id": fid, "label": label}, spoken

        # ── Fill ──────────────────────────────────────────────────────────
        value = _extract_value(transcription, current)

        action = {
            "action":   "tool_call",
            "field_id": fid,
            "label":    label,
            "value":    value,
            "type":     current.get("type", "text"),
            "reason":   "guided fill",
        }
        session.completed.append({"field": current, "value": value})
        logger.info("Guided fill: %s = '%s'", fid, value[:40] if value else "")

        if session.pending_fields:
            # More fields to go
            nxt = session.pending_fields[0]
            session.current_field  = nxt
            session.pending_fields = session.pending_fields[1:]

            next_prompt = _ask_for_field(nxt, done_idx + 1, total)
            remaining   = len(session.pending_fields) + 1

            # Progress feedback varies so it doesn't sound robotic
            confirmations = [
                f"Got it — {label} set to {value}. ",
                f"{label} is now {value}. ",
                f"Done — {label}: {value}. ",
            ]
            spoken = random.choice(confirmations) + next_prompt

        else:
            # Last field — session complete
            session.state         = GuidedFillState.DONE
            session.current_field = None
            spoken = self._build_completion(session, label, value)
            logger.info(
                "Guided fill complete — session:%s filled:%d skipped:%d auto_submit:%s",
                session_id, len(session.completed), len(session.skipped), session.auto_submit,
            )

        self._sessions[session_id] = session
        return action, spoken

    def _build_completion(
        self,
        session: GuidedFillSession,
        last_label: str | None,
        last_value: str | None,
    ) -> str:
        """Build the spoken summary when guided fill finishes."""
        n_filled  = len(session.completed)
        n_skipped = len(session.skipped)

        prefix = ""
        if last_label and last_value:
            prefix = f"Done — {last_label} set to {last_value}. "

        skip_note = (
            f" ({n_skipped} skipped)" if n_skipped else ""
        )

        if session.auto_submit:
            items = [
                f"{_human_label(c['field'])} is {c['value']}"
                for c in session.completed[:4]
            ]
            summary = "; ".join(items)
            extra   = f" and {n_filled-4} more" if n_filled > 4 else ""
            return (
                f"{prefix}All {n_filled} {'field' if n_filled==1 else 'fields'} filled"
                f"{skip_note}. "
                f"Here's a summary: {summary}{extra}. "
                "Say 'confirm' to submit or 'cancel' to review."
            )

        return (
            f"{prefix}That's all {n_filled} "
            f"{'field' if n_filled==1 else 'fields'} filled{skip_note}. "
            "Ready to submit? Say 'yes' to submit or 'done' to stop here."
        )

    # ── Session control ───────────────────────────────────────────────────

    def end_session(self, session_id: str) -> None:
        self._end_session(session_id)

    def get_current_field(self, session_id: str) -> dict | None:
        s = self._sessions.get(session_id)
        return s.current_field if s else None

    def _end_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        logger.debug("Guided fill session ended: %s", session_id)


# ── Value extraction ─────────────────────────────────────────────────────────

def _extract_value(transcription: str, f: dict) -> str:
    """
    Extract the actual value from a spoken answer.

    "The category is chairs"   → "chairs"
    "set it to today"          → "today"  (date parser handles it)
    "fifteen"                  → "15" (for number fields)
    Plain "chairs"             → "chairs"
    """
    # Strip common filler prefixes
    t = re.sub(
        r"^(it\s+is|it's|set\s+it\s+to|the\s+\w+\s+is|fill\s+it\s+as|"
        r"enter|put|write|make\s+it|use|value\s+is|i\s+want)\s+",
        "", transcription.strip(), flags=re.IGNORECASE,
    )
    t = t.strip().strip(".,")

    ftype = f.get("type", "text")

    # Number fields: convert word numbers
    if ftype == "number":
        num_map = {
            "zero":"0","one":"1","two":"2","three":"3","four":"4","five":"5",
            "six":"6","seven":"7","eight":"8","nine":"9","ten":"10",
            "eleven":"11","twelve":"12","fifteen":"15","twenty":"20",
            "fifty":"50","hundred":"100",
        }
        lower = t.lower()
        for word, digit in num_map.items():
            if lower == word:
                return digit
        # Try to extract a number
        m = re.search(r"\d+(\.\d+)?", t)
        if m:
            return m.group(0)

    # Date fields — return full transcription (date normaliser handles it)
    if ftype == "date":
        return transcription.strip()

    # Checkbox/radio — normalise yes/no
    if ftype in ("checkbox", "radio"):
        if re.search(r"\b(yes|true|on|checked|haan|ha)\b", t, re.I):
            return "true"
        if re.search(r"\b(no|false|off|nahi|nahin)\b", t, re.I):
            return "false"

    return t or transcription.strip()


# ── Singleton ─────────────────────────────────────────────────────────────────
guided_fill_manager = GuidedFillManager()