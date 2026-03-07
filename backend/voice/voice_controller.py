"""
voice_controller.py — CoPilot Voice Personality Engine (Stage 1+2+3)

Stage 1: Natural language responses, STT auto-detect, ERP field glossary
Stage 2: Guided fill, conversation memory, multilingual, proactive announcements
Stage 3: Field classifier, predictive suggestions, real-time validation,
         cross-page voice navigation, streaming TTS

Pipeline:
  Audio → STT → multilingual normalise → navigation? → intent check →
  guided_fill | undo | proactive | AI step →
  real-time validate → value suggest → response builder → TTS → WAV
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from vision_core.page_explainer import explain_page as _pe_explain_page
from voice.conversation_memory import conversation_memory, build_undo_action
from voice.field_classifier import classify_context, proactive_hints
from voice.guided_fill import guided_fill_manager
from voice.table_reader import is_table_query, answer_table_query
from voice.error_recovery import recovery_state, parse_executor_error,     parse_dom_error, build_recovery_spoken
from voice.multilingual import normalise as multilingual_normalise
from voice.proactive_announcer import (
    build_page_announcement,
    build_toggle_off_message,
)
from voice.realtime_validator import realtime_validator
from voice.value_suggester import value_suggester
from voice.voice_navigator import (
    is_navigation_intent,
    resolve_navigation,
    navigation_spoken_error,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: LANGUAGE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

_FIELD_ID_PREFIXES = re.compile(
    r"^(enter_|input_|field_|form_|txt_|tb_|cb_|sel_|dd_|chk_|rad_|lbl_)",
    re.IGNORECASE,
)
_INDIC_PARENS = re.compile(r"^\([^)]*\)\s*")
_MONTHS = [
    "January","February","March","April","May","June",
    "July","August","September","October","November","December",
]
_DAY_SUFFIXES = {1:"st", 2:"nd", 3:"rd"}


def _field_to_human(field_id: str, label: str | None = None) -> str:
    """
    Convert a field_id or raw label into a natural spoken name.

    Examples:
      "(प्रकार) Category",  "category"   → "Category"
      "enter_product_name", None          → "Product Name"
      "hsn_sac_code",       None          → "HSN SAC Code"
    """
    if label and label.strip():
        clean = _INDIC_PARENS.sub("", label.strip())
        clean = re.sub(r"[*†‡§¶]+", "", clean).strip()
        if clean and not clean.lower().startswith("field_"):
            return clean.title() if clean.islower() else clean
    fid = _FIELD_ID_PREFIXES.sub("", (field_id or "").strip())
    return fid.replace("_", " ").replace("-", " ").strip().title()


def _day_suffix(day: int) -> str:
    if 11 <= day <= 13:
        return "th"
    return _DAY_SUFFIXES.get(day % 10, "th")


def _value_to_speech(value: object, field_type: str | None = None) -> str:
    """
    Convert a raw field value into a naturally spoken string.

    "2024-01-15" → "January 15th 2024"
    "true"       → "yes"
    "chairs"     → "Chairs"
    None         → "nothing"
    """
    if value is None:
        return "nothing"
    s = str(value).strip()
    if not s:
        return "nothing"
    if s.lower() in ("true","1","yes","on"):
        return "yes"
    if s.lower() in ("false","0","no","off"):
        return "no"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        y,mo,d = int(m.group(1)),int(m.group(2)),int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{_MONTHS[mo-1]} {d}{_day_suffix(d)} {y}"
    m = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$", s)
    if m:
        d,mo,y = int(m.group(1)),int(m.group(2)),int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{_MONTHS[mo-1]} {d}{_day_suffix(d)} {y}"
    if re.match(r"^\d+(\.\d+)?$", s):
        return s
    return s[0].upper() + s[1:] if len(s) > 1 else s.upper()


def _next_required_field(
    screen_context: dict,
    just_filled_id: str | None = None,
) -> dict | None:
    """Return the next empty required field, skipping the one just filled."""
    for section in screen_context.get("sections", []):
        for f in section.get("fields", []):
            if f.get("readonly") or f.get("calculated"):
                continue
            if f.get("field_id") == just_filled_id:
                continue
            if f.get("required") and not (f.get("value") and str(f["value"]).strip()):
                return f
    return None


def _all_filled_fields(screen_context: dict) -> list[dict]:
    return [
        f
        for s in screen_context.get("sections", [])
        for f in s.get("fields", [])
        if f.get("value") and str(f["value"]).strip()
        and not f.get("readonly") and not f.get("calculated")
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: INTENT DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

_DETAIL_TRIGGERS = re.compile(
    r"\b(explain|why|detail|describe|what is|what does|how|reason|unclear|"
    r"error|validate|confirm|financial|total|amount|price|payment|"
    r"tell me about|meaning|definition|purpose)\b",
    re.IGNORECASE,
)
_SUBMIT_PATTERNS = re.compile(
    r"\b(submit|save|confirm|finalise|finalize|send|done|complete|finish)\b",
    re.IGNORECASE,
)
_UNDO_PATTERNS = re.compile(
    r"\b(undo|reverse|revert|undo that|take that back|clear that|remove that)\b",
    re.IGNORECASE,
)
_WHATS_LEFT_PATTERNS = re.compile(
    r"\b(what.?s left|what is left|remaining fields|what.?s missing|"
    r"what fields are left|what else|how many left)\b",
    re.IGNORECASE,
)
_REPLAY_PATTERNS = re.compile(
    r"\b(what did you|what did you do|what was that|repeat|say that again|"
    r"what just happened)\b",
    re.IGNORECASE,
)
# "fill everything and submit" / "complete the form and save" etc.
_EXPLAIN_INTENT_PATTERNS = re.compile(
    r"\b(what is|what does|what are|explain|describe|tell me about|"
    r"meaning of|definition of|kya hai|batao|samjhao)\b",
    re.IGNORECASE,
)

_TABLE_QUERY_PATTERNS = re.compile(
    r"\b(how many|how much|count|total|list|show me|give me|tell me|"
    r"pending|completed|cancelled|recent|last \\d|what are the|"
    r"kitne|kaay|kiti|dikha|dikhao)\b",
    re.IGNORECASE,
)

_FILL_AND_SUBMIT_PATTERNS = re.compile(
    r"(fill.*everything.*submit|fill.*all.*submit|complete.*form.*submit|"
    r"fill.*form.*save|submit.*everything|fill.*and.*submit|"
    r"fill.*required.*and.*submit|guided.*submit|sabhi.*bharo.*submit)",
    re.IGNORECASE,
)

# ── Multi-field command parser (Stage 2.3) ─────────────────────────────────
# Splits "fill name as Kalu, category as chairs, and date as today"
# into ["fill name as Kalu", "category as chairs", "date as today"]
_FILL_PREFIX = re.compile(r"^\s*(fill|set|enter|put)\s+", re.IGNORECASE)
_MULTI_SPLIT = re.compile(
    r",\s*(?:and\s+|then\s+)?|\s+and\s+|\s+then\s+",
    re.IGNORECASE,
)
_SINGLE_FILL = re.compile(
    r"\b(\w[\w\s\(\)\-]+?)\s+(?:as|to|=|:)\s+(.+)",
    re.IGNORECASE,
)


def _parse_multi_field(instruction: str) -> list[str] | None:
    """
    If instruction contains multiple fill clauses, return them as a list.
    Returns None if it looks like a single command.

    "fill name as Kalu, category as chairs, date as today"
    → ["fill name as Kalu", "category as chairs", "date as today"]
    """
    # Strip leading fill/set/enter verb
    core = _FILL_PREFIX.sub("", instruction.strip())
    parts = [p.strip() for p in _MULTI_SPLIT.split(core) if p.strip()]

    # Need at least 2 parts each matching "X as Y" pattern to be multi-field
    valid = [p for p in parts if _SINGLE_FILL.search(p)]
    if len(valid) < 2:
        return None
    # Re-add "fill" prefix to first part for consistent downstream parsing
    return [f"fill {valid[0]}"] + valid[1:]



def _requires_detail(transcription: str, action: dict, has_error: bool) -> bool:
    if _DETAIL_TRIGGERS.search(transcription):
        return True
    if has_error:
        return True
    return action.get("action") in ("error", "confirmation", "explain")


def _is_submit_intent(t: str) -> bool:
    return bool(_SUBMIT_PATTERNS.search(t))

def _is_undo(t: str) -> bool:
    return bool(_UNDO_PATTERNS.search(t))

def _is_whats_left(t: str) -> bool:
    return bool(_WHATS_LEFT_PATTERNS.search(t))

def _is_replay(t: str) -> bool:
    return bool(_REPLAY_PATTERNS.search(t))


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: RESPONSE BUILDERS
# ═══════════════════════════════════════════════════════════════════════════════

_FILL_OPENINGS = ["Got it —","Done —","All set —","Perfect —","Sure —","Absolutely —"]
_fill_idx = 0

def _next_opening() -> str:
    global _fill_idx
    o = _FILL_OPENINGS[_fill_idx % len(_FILL_OPENINGS)]
    _fill_idx += 1
    return o


def _build_short_response(
    action: dict,
    screen_context: dict | None = None,
) -> str:
    """
    1-2 sentence natural spoken confirmation + proactive next-field hint.

    tool_call  → "Got it — I've set Category to Chairs. Next up is Product Name."
    explain    → reads the message
    error      → friendly error with suggestion
    confirmation → reads summary, asks confirm/cancel
    """
    at = action.get("action","unknown")

    if at == "tool_call":
        fid   = action.get("field_id","")
        label = action.get("label","")
        value = action.get("value","")
        ftype = action.get("type","text")

        hf = _field_to_human(fid, label)
        hv = _value_to_speech(value, ftype)
        response = f"{_next_opening()} I've set {hf} to {hv}."

        if screen_context:
            nxt = _next_required_field(screen_context, just_filled_id=fid)
            if nxt:
                nn = _field_to_human(nxt.get("field_id",""), nxt.get("label",""))
                response += f" What should I fill for {nn}?"
            else:
                response += " Is there anything else you'd like me to fill?"
        return response

    if at == "explain":
        msg = (action.get("message") or "").strip()
        if len(msg) > 220:
            msg = msg[:220].rsplit(" ",1)[0] + "..."
        return msg or "Here's what I can see on this page."

    if at == "confirmation":
        return (
            f"{action.get('message','Please confirm before I proceed.')} "
            "Say 'yes' to continue or 'cancel' to stop."
        )

    if at == "error":
        reason = (action.get("reason") or "").strip()
        reason = re.sub(r"^LLM failed after \d+ attempts\.\s*Last:\s*","",reason)
        return f"Sorry, I couldn't do that. {reason}"

    return "Done."


def _build_detailed_response(
    action: dict,
    transcription: str,
    has_validation_error: bool,
    validation_errors: list[str],
    screen_context: dict | None = None,
) -> str:
    """Multi-sentence detailed response for errors, confirmations, explain."""
    at    = action.get("action","unknown")
    parts: list[str] = []

    if has_validation_error and validation_errors:
        n = len(validation_errors)
        parts.append(
            f"I found {n} validation {'issue' if n==1 else 'issues'}: "
            + " ".join(validation_errors[:2])
        )

    if at == "tool_call":
        fid   = action.get("field_id","")
        label = action.get("label","")
        value = action.get("value","")
        reason= (action.get("reason") or "").strip()
        ftype = action.get("type","text")

        hf = _field_to_human(fid, label)
        hv = _value_to_speech(value, ftype)
        parts.append(f"I'm setting the {hf} field to {hv}.")
        if reason and reason.lower() not in ("user instruction","direct match","guided fill",""):
            parts.append(f"The reason is: {reason}.")
        if screen_context:
            nxt = _next_required_field(screen_context, just_filled_id=fid)
            if nxt:
                nn = _field_to_human(nxt.get("field_id",""), nxt.get("label",""))
                parts.append(f"After this, the next required field is {nn}.")
            else:
                parts.append("Is there anything else you'd like me to fill?")

    elif at == "explain":
        msg     = (action.get("message") or "").strip()
        related = action.get("related_fields",[])
        parts.append(msg[:400] if msg else "I can see this page's structure.")
        if related:
            names = [_field_to_human(fid) for fid in related[:4]]
            parts.append(f"The relevant fields are: {', '.join(names)}.")

    elif at == "confirmation":
        msg = action.get("message","")
        parts.append(f"Before I proceed — {msg}")
        if screen_context:
            filled = _all_filled_fields(screen_context)
            if filled:
                items = [
                    f"{_field_to_human(f.get('field_id',''),f.get('label',''))} "
                    f"is {_value_to_speech(f.get('value'),f.get('type'))}"
                    for f in filled[:6]
                ]
                parts.append("Here's a summary: " + "; ".join(items) + ".")
        parts.append("Say 'yes' to confirm or 'cancel' to stop.")

    elif at == "error":
        reason = (action.get("reason") or "unknown error").strip()
        reason = re.sub(r"^LLM failed after \d+ attempts\.\s*Last:\s*","",reason)
        parts.append("I ran into a problem and couldn't complete that.")
        parts.append(f"The issue was: {reason}.")
        parts.append(
            "Try rephrasing your instruction, "
            "or say 'list fields' to hear what's available on this page."
        )

    return " ".join(parts) if parts else _build_short_response(action, screen_context)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: SUBMIT GUARD
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_submit_guard(
    action: dict,
    transcription: str,
    screen_context: dict | None = None,
) -> tuple[dict, str | None]:
    """Intercept submit actions — require explicit confirmation with value readback."""
    warning: str | None = None
    if action.get("action") == "tool_call":
        fid = (action.get("field_id") or "").lower()
        if any(kw in fid for kw in ("submit","save","confirm","send","finish","complete")):
            if _is_submit_intent(transcription):
                readback = ""
                # ── Stage 3.4: validate required fields before confirming ──
                pre_warnings: list[str] = []
                if screen_context:
                    try:
                        from voice.realtime_validator import realtime_validator as _rv
                        pre_warnings = [
                            w.spoken for w in
                            _rv.validate_before_submit(screen_context)
                        ]
                    except Exception:
                        pass
                if pre_warnings:
                    return action, " ".join(pre_warnings) + " Please fix these before submitting."

                if screen_context:
                    filled = _all_filled_fields(screen_context)
                    if filled:
                        items = [
                            f"{_field_to_human(f.get('field_id',''),f.get('label',''))}"
                            f" = {_value_to_speech(f.get('value'),f.get('type'))}"
                            for f in filled[:5]
                        ]
                        readback = " I'll submit with: " + "; ".join(items) + "."
                warning = (
                    f"I'm about to submit this form.{readback} "
                    "Say 'confirm' to proceed or 'cancel' to stop."
                )
                action = {
                    "action":            "confirmation",
                    "message":           warning,
                    "fields_to_confirm": [action.get("field_id","submit")],
                    "workflow_name":     action.get("workflow_name","unknown"),
                }
                logger.info("Submit guard triggered — confirmation with readback")
    return action, warning


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5: RESULT DATACLASS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class VoiceResult:
    transcription:          str
    normalised:             str           # after multilingual normalisation
    ai_action:              dict
    spoken_response:        str
    audio_file:             str
    detail_mode:            bool
    submit_guard_triggered: bool
    warning:                str | None
    guided_fill_active:     bool = False  # True while in guided Q&A mode
    nav_action:             bool = False  # True when action is a navigation click
    multi_fill_active:      bool = False  # True = more fills queued


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6: MAIN CONTROLLER
# ═══════════════════════════════════════════════════════════════════════════════

class VoiceController:
    """
    Full voice pipeline with personality, memory, multilingual support,
    proactive page announcements, and guided form fill.

    Audio → STT → normalise → intent → AI/memory/guided →
    response builder → TTS → WAV
    """

    def __init__(self, stt_engine, tts_engine) -> None:
        self._stt = stt_engine
        self._tts = tts_engine

    # ── Public: process one voice turn ────────────────────────────────────────

    def process(
        self,
        audio_bytes:       bytes,
        workflow_name:     str,
        screen_context:    dict,
        session_id:        str,
        ai_step_fn,
        validation_errors: list[str] | None = None,
        play_audio:        bool = False,
        audio_suffix:      str = ".wav",
    ) -> VoiceResult:
        """
        Run one complete voice turn.

        Args:
            audio_bytes:       Raw audio from browser (WAV/WebM/OGG).
            workflow_name:     Active workflow ("purchase", "supplier", "free"…).
            screen_context:    Current DOM snapshot from extractor.
            session_id:        Session ID for memory + audit.
            ai_step_fn:        fn(workflow, screen_context, instruction, session_id) → action dict.
            validation_errors: Pre-known validation errors to speak.
            play_audio:        Play WAV locally on server (dev mode).
            audio_suffix:      Audio file extension (".wav", ".webm"…).
        """
        validation_errors = validation_errors or []

        # ── 1. STT ─────────────────────────────────────────────────────────
        logger.info("Voice pipeline STT — session: %s", session_id)
        raw_transcription = self._stt.transcribe_bytes(audio_bytes, suffix=audio_suffix)
        if not raw_transcription or not raw_transcription.strip():
            raw_transcription = "(inaudible)"
            logger.warning("STT returned empty transcription")

        logger.info("Raw transcription: %s", raw_transcription[:120])

        # ── 2. Multilingual normalisation ──────────────────────────────────
        # Maps Hindi/Marathi fill-verbs and field names to English equivalents
        normalised = multilingual_normalise(raw_transcription)
        if normalised != raw_transcription:
            logger.info("Multilingual normalised: %s → %s",
                        raw_transcription[:60], normalised[:60])

        instruction = normalised  # downstream uses normalised

        # ── 3. Memory intents — no AI needed ───────────────────────────────

        # "undo that"
        if _is_undo(instruction):
            last = conversation_memory.last_fill(session_id)
            if last:
                action = build_undo_action(last)
                hf     = _field_to_human(last.field_id or "", last.field_label or "")
                spoken = f"Undoing that — I'll clear {hf}."
                audio_file = self._tts.synthesise(spoken, play=play_audio)
                conversation_memory.add_turn(session_id, instruction, action, spoken)
                return VoiceResult(
                    transcription=raw_transcription, normalised=normalised,
                    ai_action=action, spoken_response=spoken, audio_file=audio_file,
                    detail_mode=False, submit_guard_triggered=False, warning=None,
                guided_fill_active=False, multi_fill_active=False,
                )
            else:
                spoken = "There's nothing to undo yet in this session."
                audio_file = self._tts.synthesise(spoken, play=play_audio)
                return VoiceResult(
                    transcription=raw_transcription, normalised=normalised,
                    ai_action={"action":"explain","message":spoken},
                    spoken_response=spoken, audio_file=audio_file,
                    detail_mode=False, submit_guard_triggered=False, warning=None,
                guided_fill_active=False, multi_fill_active=False,
                )

        # "what's left?"
        if _is_whats_left(instruction):
            spoken = _build_whats_left_response(screen_context)
            audio_file = self._tts.synthesise(spoken, play=play_audio)
            return VoiceResult(
                transcription=raw_transcription, normalised=normalised,
                ai_action={"action":"explain","message":spoken},
                spoken_response=spoken, audio_file=audio_file,
                detail_mode=False, submit_guard_triggered=False, warning=None,
            guided_fill_active=False, multi_fill_active=False,
            )

        # "what did you do?" / "repeat"
        if _is_replay(instruction):
            last = conversation_memory.last_turn(session_id)
            if last:
                spoken = f"Last time, {last.spoken}"
            else:
                spoken = "I haven't done anything yet this session."
            audio_file = self._tts.synthesise(spoken, play=play_audio)
            return VoiceResult(
                transcription=raw_transcription, normalised=normalised,
                ai_action={"action":"explain","message":spoken},
                spoken_response=spoken, audio_file=audio_file,
                detail_mode=False, submit_guard_triggered=False, warning=None,
            guided_fill_active=False, multi_fill_active=False,
            )

        # ── 3b. Cross-page navigation (Stage 3.5) ────────────────────────
        # Explicitly exclude fill/set/enter commands — "fill supplier as ABC"
        # must never trigger navigation even if "supplier" is a nav link name.
        _is_fill_command = bool(re.search(
            r"\b(fill|set|enter|put|type|write|bharo|ghala|kar|likho)\b",
            instruction, re.IGNORECASE,
        ))
        if not _is_fill_command and is_navigation_intent(instruction):
            nav_action = resolve_navigation(instruction, screen_context)
            if nav_action:
                spoken = nav_action.pop("spoken", "Navigating now.")
                audio_file = self._tts.synthesise(spoken, play=play_audio)
                conversation_memory.add_turn(session_id, instruction, nav_action, spoken)
                return VoiceResult(
                    transcription=raw_transcription, normalised=normalised,
                    ai_action=nav_action, spoken_response=spoken,
                    audio_file=audio_file, detail_mode=False,
                    submit_guard_triggered=False, warning=None,
                    nav_action=True,
                    guided_fill_active=False, multi_fill_active=False,
                )
            else:
                # Navigation intent but no matching button — tell user with options
                spoken = navigation_spoken_error(instruction, context=screen_context)
                audio_file = self._tts.synthesise(spoken, play=play_audio)
                return VoiceResult(
                    transcription=raw_transcription, normalised=normalised,
                    ai_action={"action": "explain", "message": spoken},
                    spoken_response=spoken, audio_file=audio_file,
                    detail_mode=False, submit_guard_triggered=False, warning=None,
                guided_fill_active=False, multi_fill_active=False,
                )

        # ── 3b. Error recovery answer ──────────────────────────────────────
        # If a previous fill failed and we offered a fix, the next voice input
        # is the user's response (yes/no/new value). Route it here first.
        # ── 3a. Multi-fill queue drain ──────────────────────────────────────────
        # If a previous multi-fill queued extra fills, pop the next one
        # and skip all AI/NLP steps — just execute it directly.
        _mf_pending = getattr(self, "_multi_fill_queue", [])
        if _mf_pending:
            next_fill = _mf_pending[0]
            self._multi_fill_queue = _mf_pending[1:]
            fid   = next_fill.get("field_id","")
            label = next_fill.get("label","") or fid
            val   = next_fill.get("value","")
            n_left = len(self._multi_fill_queue)
            spoken = f"{'Got it — ' if not n_left else ''}{label} set to {val}."
            if n_left:
                next_lbl = self._multi_fill_queue[0].get("label","") if self._multi_fill_queue else ""
                spoken += f" Next: {next_lbl}." if next_lbl else ""
            audio_file = self._tts.synthesise(spoken, play=play_audio)
            return VoiceResult(
                transcription=instruction, normalised=instruction,
                ai_action=next_fill, spoken_response=spoken, audio_file=audio_file,
                detail_mode=False, submit_guard_triggered=False, warning=None,
                guided_fill_active=guided_fill_manager.is_in_guided_session(session_id),
                multi_fill_active=bool(getattr(self, '_multi_fill_queue', [])),
            )

        if recovery_state.is_in_recovery(session_id):
            action, spoken = recovery_state.build_retry_action(session_id, instruction)
            # Empty spoken + no action = user issued a different command — fall through
            if spoken == "" and action is None:
                recovery_state.clear(session_id)  # ensure cleared
                pass  # fall through to normal pipeline below
            elif action and action.get("action") == "tool_call":
                # Retry fill with corrected value
                audio_file = self._tts.synthesise(spoken, play=play_audio)
                return VoiceResult(
                    transcription=raw_transcription, normalised=normalised,
                    ai_action=action, spoken_response=spoken, audio_file=audio_file,
                    detail_mode=False, submit_guard_triggered=False, warning=None,
                    guided_fill_active=guided_fill_manager.is_in_guided_session(session_id),
                    multi_fill_active=False,
                )
            elif spoken:
                # No action (skip / need more info) — just speak
                audio_file = self._tts.synthesise(spoken, play=play_audio)
                return VoiceResult(
                    transcription=raw_transcription, normalised=normalised,
                    ai_action={"action": "explain", "message": spoken},
                    spoken_response=spoken, audio_file=audio_file,
                    detail_mode=False, submit_guard_triggered=False, warning=None,
                    guided_fill_active=guided_fill_manager.is_in_guided_session(session_id),
                    multi_fill_active=False,
                )

        # ── 3c. Fill-everything-and-submit (Stage 3.3) ──────────────────
        if _FILL_AND_SUBMIT_PATTERNS.search(instruction):
            # Start guided fill; set a flag so guided_fill knows to auto-submit at end
            spoken = guided_fill_manager.start_session(
                session_id, screen_context, workflow_name, auto_submit=True
            )
            audio_file = self._tts.synthesise(spoken, play=play_audio)
            return VoiceResult(
                transcription=raw_transcription, normalised=normalised,
                ai_action={"action": "explain", "message": spoken},
                spoken_response=spoken, audio_file=audio_file,
                detail_mode=False, submit_guard_triggered=False, warning=None,
                guided_fill_active=True,
                multi_fill_active=False,
            )

        # ── 4. Guided fill — start ─────────────────────────────────────────
        if guided_fill_manager.is_guided_trigger(instruction):
            spoken = guided_fill_manager.start_session(
                session_id, screen_context, workflow_name
            )
            audio_file = self._tts.synthesise(spoken, play=play_audio)
            return VoiceResult(
                transcription=raw_transcription, normalised=normalised,
                ai_action={"action":"explain","message":spoken},
                spoken_response=spoken, audio_file=audio_file,
                detail_mode=False, submit_guard_triggered=False, warning=None,
                guided_fill_active=True,
                multi_fill_active=False,
            )

        # ── 5. Guided fill — answer ────────────────────────────────────────
        if guided_fill_manager.is_in_guided_session(session_id):
            action, spoken = guided_fill_manager.handle_answer(session_id, instruction)

            # Session cancelled or completed
            if action is None or action.get("action") in ("explain", None):
                guided_fill_manager.end_session(session_id)
                audio_file = self._tts.synthesise(spoken, play=play_audio)
                return VoiceResult(
                    transcription=raw_transcription, normalised=normalised,
                    ai_action={"action": "explain", "message": spoken},
                    spoken_response=spoken, audio_file=audio_file,
                    detail_mode=False, submit_guard_triggered=False, warning=None,
                    guided_fill_active=False,
                    multi_fill_active=False,
                )

            # Skip — no DOM action, but session continues to next field
            if action.get("action") == "skip":
                still_active = guided_fill_manager.is_in_guided_session(session_id)
                audio_file   = self._tts.synthesise(spoken, play=play_audio)
                return VoiceResult(
                    transcription=raw_transcription, normalised=normalised,
                    ai_action={"action": "explain", "message": spoken},
                    spoken_response=spoken, audio_file=audio_file,
                    detail_mode=False, submit_guard_triggered=False, warning=None,
                    guided_fill_active=still_active,
                    multi_fill_active=False,
                )

            # Fill action — execute in tab, session continues
            audio_file = self._tts.synthesise(spoken, play=play_audio)
            conversation_memory.add_turn(session_id, instruction, action, spoken)
            still_active = guided_fill_manager.is_in_guided_session(session_id)
            return VoiceResult(
                transcription=raw_transcription, normalised=normalised,
                ai_action=action, spoken_response=spoken, audio_file=audio_file,
                detail_mode=False, submit_guard_triggered=False, warning=None,
                guided_fill_active=still_active,
                multi_fill_active=False,
            )

        # ── 5c. Multi-field command (Stage 2.3) ───────────────────────────
        # "fill name as Kalu, category as chairs, date as today"
        # → split into sub-instructions → process first, return rest as pending
        multi_parts = _parse_multi_field(instruction)
        if multi_parts and len(multi_parts) >= 2:
            logger.info("Multi-field command: %d parts", len(multi_parts))
            # Run the AI step only for the FIRST field; the others come as
            # separate voice turns after the user hears the first confirmation.
            # We append a spoken hint so the user knows more fields are queued.
            instruction = multi_parts[0]
            _pending_hint = (
                f" I'll also fill {len(multi_parts)-1} more "
                f"{'field' if len(multi_parts)==2 else 'fields'} after this."
            ) if len(multi_parts) > 1 else ""
        else:
            _pending_hint = ""

        # ── 6. AI reasoning step ───────────────────────────────────────────
        # ── 5a. Table / list query short-circuit ──────────────────────────────
        # "how many pending orders", "show me last 5 orders"
        # Answered instantly from context["tables"] — zero LLM
        if _TABLE_QUERY_PATTERNS.search(instruction) or _EXPLAIN_INTENT_PATTERNS.search(instruction):
            if is_table_query(instruction, screen_context):
                try:
                    answer = answer_table_query(instruction, screen_context)
                    if answer and len(answer.strip()) > 5:
                        audio_file = self._tts.synthesise(answer, play=play_audio)
                        conversation_memory.add_turn(
                            session_id, instruction,
                            {"action": "explain", "message": answer}, answer,
                        )
                        return VoiceResult(
                            transcription=raw_transcription, normalised=normalised,
                            ai_action={"action": "explain", "message": answer},
                            spoken_response=answer, audio_file=audio_file,
                            detail_mode=False, submit_guard_triggered=False, warning=None,
                        guided_fill_active=False, multi_fill_active=False,
                        )
                except Exception as _tbl_err:
                    logger.debug("table_reader error: %s", _tbl_err)

        # ── 5b. Explain intent → page_explainer short-circuit (Stage 2.4) ──────
        # "what is HSN code", "explain supplier field", "what does CGST mean"
        # → answered from glossary or DOM instantly, no LLM call needed.
        # Only falls through to AI if page_explainer returns nothing useful.
        if _EXPLAIN_INTENT_PATTERNS.search(instruction):
            try:
                insight = _pe_explain_page(
                    context            = screen_context,
                    user_instruction   = instruction,
                    ollama_generate_fn = None,  # None = skip LLM, use glossary/DOM only
                )
                # raw_explanation is the voice-ready answer from glossary or DOM tier
                answer = getattr(insight, "raw_explanation", None)
                if answer and len(answer.strip()) > 10:
                    audio_file = self._tts.synthesise(answer, play=play_audio)
                    conversation_memory.add_turn(
                        session_id, instruction,
                        {"action": "explain", "message": answer}, answer,
                    )
                    return VoiceResult(
                        transcription=raw_transcription, normalised=normalised,
                        ai_action={"action": "explain", "message": answer},
                        spoken_response=answer, audio_file=audio_file,
                        detail_mode=False, submit_guard_triggered=False, warning=None,
                    guided_fill_active=False, multi_fill_active=False,
                    )
            except Exception as _explain_err:
                logger.debug("page_explainer short-circuit error: %s", _explain_err)
            # Not answered by glossary/DOM — fall through to AI step below

        logger.info("Voice pipeline AI — instruction: %s", instruction[:80])
        action: dict = ai_step_fn(
            workflow=workflow_name,
            screen_context=screen_context,
            instruction=instruction,
            session_id=session_id,
        )

        # ── 6a. Multi-fill action from Tier 1.5 / LLM array ─────────────────
        # Returned when sentence_parser or LLM found multiple slots.
        # Queue all fills; return first one NOW, rest queued for next turns.
        if action.get("action") == "multi_fill":
            fills = action.get("fills", [])
            if not fills:
                action = {"action": "explain", "message": "I couldn't identify any fields to fill."}
            else:
                # Enrich each fill with DOM label
                enriched = []
                for fill in fills:
                    fid = fill.get("field_id","")
                    if not fill.get("label"):
                        for sec in screen_context.get("sections",[]):
                            for f in sec.get("fields",[]):
                                if f.get("field_id") == fid:
                                    fill = {**fill, "label": f.get("label",""), "type": f.get("type","text")}
                                    break
                    enriched.append(fill)

                # Queue fills 2..N into multi-fill pending queue
                if len(enriched) > 1:
                    _mf_queue = getattr(self, "_multi_fill_queue", None)
                    if _mf_queue is None:
                        self._multi_fill_queue = enriched[1:]
                    else:
                        self._multi_fill_queue = enriched[1:]

                    n_more = len(enriched) - 1
                    _pending_hint = (
                        f" I'll fill {n_more} more "
                        f"{'field' if n_more == 1 else 'fields'} after this."
                    )

                # First fill becomes the action for this turn
                action = enriched[0]
                logger.info(
                    "Multi-fill: %d fills queued, starting with field_id='%s'",
                    len(fills), action.get("field_id","")
                )

        # Also drain LLM batch queue if present
        try:
            from reasoning_layer.validator import pop_llm_batch
            llm_batch = pop_llm_batch()
            if llm_batch and action.get("action") == "tool_call":
                existing = getattr(self, "_multi_fill_queue", [])
                self._multi_fill_queue = llm_batch + existing
                if not _pending_hint:
                    n = len(llm_batch)
                    _pending_hint = f" I'll fill {n} more {'field' if n==1 else 'fields'} after this."
        except Exception:
            pass

        # Enrich tool_call with label+type from DOM for better speech
        if action.get("action") == "tool_call" and not action.get("label"):
            fid = action.get("field_id","")
            for section in screen_context.get("sections",[]):
                for f in section.get("fields",[]):
                    if f.get("field_id") == fid:
                        action = {**action, "label": f.get("label",""), "type": f.get("type","text")}
                        break

        # ── 6b. Semantic field classification (Stage 3.1) ─────────────────
        # Annotate the action with the semantic FieldClass so response builders
        # can give smarter confirmations (e.g. "I've set the tax rate to 18%")
        if action.get("action") == "tool_call":
            classifications = classify_context(screen_context)
            fid = action.get("field_id","")
            if fid in classifications:
                fc = classifications[fid]
                action = {**action, "field_class": fc.name}
                logger.debug("FieldClass for %s → %s", fid, fc.name)

        # ── 7. Submit guard ────────────────────────────────────────────────
        action, submit_warning = _apply_submit_guard(action, instruction, screen_context)
        submit_triggered = submit_warning is not None

        # ── 8. Build spoken response ───────────────────────────────────────
        has_error = bool(validation_errors)
        detail    = _requires_detail(instruction, action, has_error)

        if detail:
            spoken = _build_detailed_response(
                action=action,
                transcription=instruction,
                has_validation_error=has_error,
                validation_errors=validation_errors,
                screen_context=screen_context,
            )
        else:
            spoken = _build_short_response(action, screen_context)

        if submit_warning and submit_warning not in spoken:
            spoken = submit_warning + " " + spoken

        logger.info("Voice response [%s] %d chars: %s",
                    "detail" if detail else "short", len(spoken), spoken[:100])

        # ── 8b. Real-time validation (Stage 3.4) ─────────────────────────
        # Run after filling a field — warn immediately before next step
        if action.get("action") == "tool_call" and not submit_triggered:
            fid   = action.get("field_id", "")
            label = action.get("label", "")
            val   = action.get("value", "")
            val_warnings = realtime_validator.validate_fill(
                fid, label, val,
                action.get("type", "text"),
                action.get("placeholder", "") or "",
            )
            if val_warnings:
                warn_text = realtime_validator.spoken_summary(val_warnings)
                # Blocking warnings enter recovery mode so the next voice
                # input is routed as a correction, not a new command
                blocking = [w for w in val_warnings if w.blocking]
                if blocking:
                    from voice.error_recovery import ErrorRecovery, ErrorKind, RecoveryAction
                    rec = ErrorRecovery(
                        error_kind=ErrorKind.DOM_VALIDATION,
                        field_id=fid, field_label=label,
                        original_value=val,
                        spoken_error=warn_text,
                        spoken_offer=f"What should I use for {label} instead?",
                        recovery=RecoveryAction("ask", fid, label),
                        is_blocking=True,
                    )
                    recovery_state.set_pending(session_id, rec)
                spoken = spoken + " " + warn_text if warn_text else spoken

        # ── 8c. Predictive value suggestions (Stage 3.2) ──────────────────
        # Suggest values for related empty fields based on what just changed
        if action.get("action") == "tool_call" and not submit_triggered:
            fid   = action.get("field_id", "")
            label = action.get("label", "")
            val   = action.get("value", "")
            value_suggester.record(fid, label, val, screen_context)
            suggestions = value_suggester.suggest(fid, label, val, screen_context)
            if suggestions:
                # Speak first suggestion as a trailing hint
                s = suggestions[0]
                spoken = spoken + " " + s.reason

        # ── 8d. Append multi-field pending hint if present (Stage 2.3) ──────
        if _pending_hint:
            spoken = spoken + _pending_hint

        # ── 9. TTS ─────────────────────────────────────────────────────────
        audio_file = self._tts.synthesise(spoken, play=play_audio)

        # ── 10. Record in conversation memory ─────────────────────────────
        conversation_memory.add_turn(session_id, instruction, action, spoken)

        _mf_active = bool(getattr(self, "_multi_fill_queue", []))
        return VoiceResult(
            transcription=raw_transcription,
            normalised=normalised,
            ai_action=action,
            spoken_response=spoken,
            audio_file=audio_file,
            detail_mode=detail,
            submit_guard_triggered=submit_triggered,
            warning=submit_warning,
            multi_fill_active=_mf_active,
            guided_fill_active=guided_fill_manager.is_in_guided_session(session_id),
        )

    # ── Public: proactive page announcement ───────────────────────────────────

    def announce_page(
        self,
        screen_context: dict,
        url:            str = "",
        toggle_on:      bool = True,
        play_audio:     bool = False,
        on_chunk:       object = None,
    ) -> str:
        """
        Speak a page-load announcement.
        Called by popup when the toggle is switched ON or a new page loads.

        For long announcements (>120 chars) uses streaming TTS so the first
        sentence plays within ~300ms instead of waiting for the full synthesis.

        on_chunk: optional callback(wav_path) called per sentence chunk.
                  When provided, streaming is used regardless of length.

        Returns the first (or only) audio file path.
        """
        spoken = build_page_announcement(screen_context, url=url, enabled=toggle_on)

        # Proactive field hints (Stage 3.1)
        hints = proactive_hints(screen_context)
        if hints and not toggle_on:
            # Add one hint on navigation (don't pile them all on toggle-on)
            spoken = spoken + " " + hints[0]

        logger.info("Proactive announcement (%d chars): %s", len(spoken), spoken[:100])

        if on_chunk or len(spoken) > 120:
            from voice.streaming_tts import get_streaming_tts
            st = get_streaming_tts()
            paths = st.speak_streaming(spoken, on_chunk or (lambda _: None))
            return paths[0] if paths else self._tts.synthesise(spoken, play=play_audio)

        return self._tts.synthesise(spoken, play=play_audio)

    def announce_toggle_off(self, play_audio: bool = False) -> str:
        """Speak the toggle-off farewell message. Returns audio path."""
        spoken = build_toggle_off_message()
        return self._tts.synthesise(spoken, play=play_audio)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7: HELPER — "What's left?"
# ═══════════════════════════════════════════════════════════════════════════════

def _build_whats_left_response(screen_context: dict) -> str:
    """Build a spoken list of unfilled required fields."""
    missing = []
    for section in screen_context.get("sections",[]):
        for f in section.get("fields",[]):
            if (f.get("required")
                    and not f.get("readonly")
                    and not (f.get("value") and str(f["value"]).strip())):
                name = _field_to_human(f.get("field_id",""), f.get("label",""))
                missing.append(name)

    if not missing:
        return "All required fields are filled. You're ready to submit!"

    n = len(missing)
    names = ", ".join(missing[:5])
    extra = f" and {n-5} more" if n > 5 else ""
    return (
        f"There {'is' if n==1 else 'are'} {n} required "
        f"{'field' if n==1 else 'fields'} still empty: {names}{extra}. "
        "Say 'fill required fields' to go through them one by one."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8: SINGLETON
# ═══════════════════════════════════════════════════════════════════════════════

_voice_controller: VoiceController | None = None


def get_voice_controller() -> VoiceController:
    """Return (or create on first call) the global VoiceController singleton."""
    global _voice_controller
    if _voice_controller is None:
        from voice.stt_engine import stt_engine
        from voice.tts_engine import tts_engine
        _voice_controller = VoiceController(stt_engine, tts_engine)
    return _voice_controller