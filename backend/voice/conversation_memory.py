"""
conversation_memory.py — Per-session conversational state for CoPilot Voice

Enables:
  "undo that"           → reverses the last fill action
  "what did you do?"    → replays last action in speech
  "fill required fields" → guided Q&A for all empty required fields
  "what's left?"        → lists unfilled required fields

Stored per session_id. Each session holds a rolling window of turns.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MAX_TURNS_PER_SESSION = 50   # cap memory per session


@dataclass
class ConversationTurn:
    """One complete voice interaction — what was said, what was done."""
    instruction:  str               # user's spoken instruction
    action_type:  str               # tool_call | explain | confirmation | error
    field_id:     str | None        # which field was filled (if tool_call)
    field_label:  str | None        # human-readable label (for readback)
    value:        Any               # value that was set
    spoken:       str               # what the assistant said
    timestamp:    float = field(default_factory=time.monotonic)


class ConversationMemory:
    """
    Lightweight per-session conversation history.

    Sessions are ephemeral — lost on server restart.
    Good enough for a single form-filling session (minutes).
    """

    def __init__(self) -> None:
        # session_id → list of ConversationTurn
        self._sessions: dict[str, list[ConversationTurn]] = defaultdict(list)

    def add_turn(
        self,
        session_id: str,
        instruction: str,
        action:      dict,
        spoken:      str,
    ) -> None:
        """Record one completed voice interaction."""
        turn = ConversationTurn(
            instruction = instruction,
            action_type = action.get("action", "unknown"),
            field_id    = action.get("field_id"),
            field_label = action.get("label"),
            value       = action.get("value"),
            spoken      = spoken,
        )
        turns = self._sessions[session_id]
        turns.append(turn)
        # Rolling window — keep only the most recent turns
        if len(turns) > MAX_TURNS_PER_SESSION:
            self._sessions[session_id] = turns[-MAX_TURNS_PER_SESSION:]
        logger.debug(
            "Memory: session %s now has %d turns", session_id, len(self._sessions[session_id])
        )

    def last_fill(self, session_id: str) -> ConversationTurn | None:
        """Return the most recent tool_call turn, or None."""
        for turn in reversed(self._sessions.get(session_id, [])):
            if turn.action_type == "tool_call" and turn.field_id:
                return turn
        return None

    def last_turn(self, session_id: str) -> ConversationTurn | None:
        """Return the very last turn regardless of type."""
        turns = self._sessions.get(session_id, [])
        return turns[-1] if turns else None

    def all_turns(self, session_id: str) -> list[ConversationTurn]:
        return list(self._sessions.get(session_id, []))

    def clear(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


# ── Undo helper ────────────────────────────────────────────────────────────────

def build_undo_action(last_fill: ConversationTurn) -> dict:
    """
    Build a tool_call action that clears the last filled field.
    Returns an action dict with value="" to clear the field.
    """
    return {
        "action":   "tool_call",
        "field_id": last_fill.field_id,
        "label":    last_fill.field_label or last_fill.field_id,
        "value":    "",
        "reason":   "undo",
    }


# ── Module-level singleton ─────────────────────────────────────────────────────
conversation_memory = ConversationMemory()