"""
event_loop.py — Navix Vision Continuous Perception Loop

This is the core architectural upgrade from request/response to streaming.

Analogy to Copilot Voice:
  Voice:  Mic stream → VAD → Whisper → LLM → TTS
  Vision: DOM stream → VisualVAD → Explainer/Actor → TTS/Executor

The loop runs as a background asyncio task. The Chrome extension pushes DOM
snapshots via WebSocket. The loop:
  1. Receives snapshots on an async queue (never blocks)
  2. Passes them through VisualVAD (cheap, synchronous)
  3. If interesting: hands off to LLM reasoning in a thread pool (non-blocking)
  4. Routes the result back to:
     a. The voice controller (TTS explanation)
     b. The executor (fill/click/navigate in the browser)
     c. The audit logger (JSONL trace)

Drop-stale policy (critical for real-time):
  - The snapshot queue has maxsize=1
  - Putting a new snapshot in a full queue DROPS the old one
  - This means: if the LLM is busy, we always reason about the NEWEST state

Circuit breakers (same as AutonomousExecutor):
  - Max 10 autonomous actions per user task
  - Stops on error action
  - Stops on confirmation required
  - Infinite loop detection via state hash
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from asyncio import Queue
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

from vision_core.visual_vad import VisualVAD, VADResult, visual_vad

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────
MAX_QUEUE_SIZE       = 1      # Drop stale: only keep newest snapshot
LLM_TIMEOUT_SECS     = 25    # Hard timeout per LLM call
MAX_AUTO_ACTIONS     = 10    # Circuit breaker: max sequential actions
LOOP_DETECT_WINDOW   = 5     # Hash window for loop detection


# ── Event types from extension ─────────────────────────────────────────────

class EventType(str, Enum):
    DOM_SNAPSHOT   = "dom_snapshot"    # Periodic/mutation-triggered snapshot
    NAVIGATION     = "navigation"      # Page load / pushState / popState
    VOICE_COMMAND  = "voice_command"   # User spoke via mic
    USER_TEXT      = "user_text"       # User typed in Tauri chat
    CONFIRMATION   = "confirmation"    # User confirmed/cancelled a modal


@dataclass
class CopilotEvent:
    event_type:     EventType
    session_id:     str
    screen_context: dict
    payload:        dict = field(default_factory=dict)
    timestamp:      float = field(default_factory=time.monotonic)
    url:            str | None = None


# ── Loop state ─────────────────────────────────────────────────────────────

@dataclass
class LoopState:
    prev_context:    dict | None = None
    prev_url:        str | None  = None
    action_count:    int         = 0
    session_id:      str         = ""
    pending_confirm: dict | None = None
    seen_hashes:     list[str]   = field(default_factory=list)
    workflow_name:   str         = "purchase"
    user_instruction: str        = ""
    # BUG 4 FIX: Track pending navigation so announcement waits for fresh context.
    # When a navigation event fires, the screen_context is still the OLD page.
    # We store the target URL and wait for the next dom_snapshot (fresh context)
    # before announcing. This prevents "I can see a Sales Order form" on a login page.
    pending_nav_url: str | None  = None
    pending_nav_session: str | None = None


# ── Result routing ─────────────────────────────────────────────────────────

@dataclass
class LoopOutput:
    """
    Produced by the loop after each reasoning cycle.
    Routed to: WebSocket clients, TTS engine, Tauri app.
    """
    session_id:     str
    action:         dict
    vad_result:     VADResult
    spoken_text:    str
    tts_audio_path: str | None
    execute_in_tab: bool         # True = send fill/click to extension
    latency_ms:     float


# ── The Event Loop ─────────────────────────────────────────────────────────

class CopilotVisionLoop:
    """
    Continuous perception loop for Navix Vision.

    Start with: await loop.start()
    Push events: await loop.push_event(event)
    Subscribe to outputs: loop.add_output_handler(async_fn)
    Stop with: await loop.stop()
    """

    def __init__(
        self,
        reasoning_fn: Callable[..., dict],
        explainer_fn: Callable[..., Any],
        tts_fn:       Callable[[str], str] | None = None,
        permission_fn: Callable[[str], dict] | None = None,
        audit_fn:     Callable[..., None] | None = None,
        vad:          VisualVAD | None = None,
        executor:     ThreadPoolExecutor | None = None,
    ):
        """
        Args:
            reasoning_fn:  fn(workflow, context, instruction, session_id) → action dict
            explainer_fn:  fn(context, instruction) → PageInsight
            tts_fn:        fn(text) → audio_path (None = voice disabled)
            permission_fn: fn(tool_name) → {"outcome": "allowed"|"blocked"|"requires_confirmation"}
            audit_fn:      fn(**kwargs) → None  (fire-and-forget audit write)
            vad:           VisualVAD instance (defaults to global singleton)
            executor:      ThreadPoolExecutor for blocking LLM calls
        """
        self._reasoning   = reasoning_fn
        self._explainer   = explainer_fn
        self._tts         = tts_fn
        self._permission  = permission_fn
        self._audit       = audit_fn
        self._vad         = vad or visual_vad
        self._executor    = executor or ThreadPoolExecutor(max_workers=2,
                                                           thread_name_prefix="navix-llm")

        # Drop-stale queue: maxsize=1
        self._queue: Queue[CopilotEvent] = Queue(maxsize=MAX_QUEUE_SIZE)

        self._output_handlers: list[Callable[[LoopOutput], Awaitable[None]]] = []
        self._state   = LoopState()
        self._running = False
        self._task:   asyncio.Task | None = None

    # ── Public API ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the continuous perception loop as a background task."""
        if self._running:
            return
        self._running = True
        self._task    = asyncio.create_task(self._loop(), name="navix-vision-loop")
        logger.info("Navix Vision loop started")

    async def stop(self) -> None:
        """Gracefully stop the loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Navix Vision loop stopped")

    async def push_event(self, event: CopilotEvent) -> bool:
        """
        Push a new event into the loop.

        Returns True if queued, False if the queue was full (old event dropped).
        Drop-stale: if queue is full, removes old item before inserting new.
        """
        if self._queue.full():
            try:
                self._queue.get_nowait()
                logger.debug("Drop-stale: evicted old event from queue")
            except asyncio.QueueEmpty:
                pass

        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            logger.warning("Queue full after drain — dropping event")
            return False

    def add_output_handler(self, handler: Callable[[LoopOutput], Awaitable[None]]) -> None:
        """Register a callback that receives every LoopOutput."""
        self._output_handlers.append(handler)

    def set_workflow(self, workflow_name: str) -> None:
        self._state.workflow_name = workflow_name

    def set_instruction(self, instruction: str) -> None:
        self._state.user_instruction = instruction

    def reset_for_new_task(self, session_id: str, workflow: str, instruction: str) -> None:
        """Call this when the user gives a new voice/text command."""
        self._state.action_count    = 0
        self._state.session_id      = session_id
        self._state.workflow_name   = workflow
        self._state.user_instruction = instruction
        self._state.seen_hashes     = []
        self._state.pending_confirm = None
        logger.info("Loop state reset — session: %s, workflow: %s", session_id, workflow)

    # ── Core loop ───────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while self._running:
            try:
                # Wait for next event with timeout so we can check self._running
                try:
                    event = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                t_start = time.monotonic()
                await self._handle_event(event, t_start)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Vision loop uncaught exception: %s", exc, exc_info=True)
                await asyncio.sleep(0.1)   # Brief pause before retry

    async def _handle_event(self, event: CopilotEvent, t_start: float) -> None:
        state = self._state

        # ── 1. VAD — is this event interesting? ─────────────────────────────
        vad_result = self._vad.compare(
            prev_context=state.prev_context,
            curr_context=event.screen_context,
            prev_url=state.prev_url,
            curr_url=event.url,
        )

        # Always update prev state
        state.prev_context = event.screen_context
        state.prev_url     = event.url

        # Voice/text commands always bypass VAD threshold
        is_user_command = event.event_type in (
            EventType.VOICE_COMMAND, EventType.USER_TEXT
        )

        if not vad_result.interesting and not is_user_command:
            logger.debug(
                "VAD: not interesting (score=%.2f) — %s",
                vad_result.salience_score, vad_result.reason,
            )
            return

        logger.info(
            "VAD: interesting! type=%s score=%.2f — %s",
            vad_result.change_type.value, vad_result.salience_score, vad_result.reason,
        )

        # ── 2. Loop detection ────────────────────────────────────────────────
        ctx_hash = _context_hash(event.screen_context, state.user_instruction)
        if ctx_hash in state.seen_hashes[-LOOP_DETECT_WINDOW:]:
            logger.warning("Loop detected — same state hash seen recently, skipping")
            return
        state.seen_hashes.append(ctx_hash)

        # ── 3. Circuit breaker ───────────────────────────────────────────────
        if state.action_count >= MAX_AUTO_ACTIONS and not is_user_command:
            logger.warning("Circuit breaker: max auto actions (%d) reached", MAX_AUTO_ACTIONS)
            return

        # ── 4. Determine instruction ─────────────────────────────────────────
        instruction = (
            event.payload.get("text", "") or
            event.payload.get("transcription", "") or
            state.user_instruction or
            "Explain this page and fill any required fields."
        )

                # ── 4b. Navigation → proactive_announcer (skip LLM entirely) ──────────
        # Navigation events speak the page summary instantly — no LLM call.
        # The LLM path takes 30-60s and returns wrong answers on empty context.
        _is_nav = (
            event.event_type == EventType.NAVIGATION
            or vad_result.change_type.value == "navigation"
        ) and not is_user_command
        _no_user_instr = not event.payload.get("text", "").strip()

        if _is_nav and _no_user_instr:
            # BUG 4 FIX: Don't announce immediately on navigation — the screen_context
            # at this moment is still the OLD page's DOM. Store the target URL and
            # session, then wait for the next dom_snapshot which will carry the real
            # new-page context. The announcement fires from that next event instead.
            _target_url = event.url or ""
            _sid = event.session_id or state.session_id or f"auto-{int(time.time())}"

            # If this is a login/auth page, skip announcement entirely
            from vision_core.visual_vad import VisualVAD as _VVAD
            if _VVAD._SUPPRESS_URL_PATTERNS.search(_target_url):
                logger.info("Navigation to auth/login page — suppressing announcement: %s", _target_url)
                state.pending_nav_url = None
                state.pending_nav_session = None
                return

            # Store pending nav — announcement will fire on next dom_snapshot
            state.pending_nav_url     = _target_url
            state.pending_nav_session = _sid
            logger.info("Navigation detected — deferring announcement until fresh DOM: %s", _target_url)
            return  # ← wait for next dom_snapshot with real context

        # ── 4c. Deferred navigation announcement (fires on first post-nav snapshot) ──
        if (
            state.pending_nav_url
            and event.event_type == EventType.DOM_SNAPSHOT
            and not is_user_command
        ):
            _target_url = state.pending_nav_url
            _sid        = state.pending_nav_session or event.session_id or f"auto-{int(time.time())}"
            # Clear pending — we consume it now
            state.pending_nav_url     = None
            state.pending_nav_session = None

            _spoken = ""
            try:
                from voice.proactive_announcer import build_page_announcement
                # Now event.screen_context is the FRESH new page DOM
                _spoken = build_page_announcement(
                    context=event.screen_context,
                    url=_target_url,
                    enabled=False,
                )
            except Exception as _e:
                logger.warning("Proactive announcer error: %s", _e)

            _tts_path = None
            if self._tts and _spoken:
                try:
                    _tts_path = await asyncio.get_running_loop().run_in_executor(
                        self._executor, self._tts, _spoken,
                    )
                except Exception as _e:
                    logger.warning("Proactive TTS failed: %s", _e)

            _latency = round((time.monotonic() - t_start) * 1000, 1)
            _output  = LoopOutput(
                session_id=_sid,
                action={"action": "explain", "message": _spoken},
                vad_result=vad_result,
                spoken_text=_spoken,
                tts_audio_path=_tts_path,
                execute_in_tab=False,
                latency_ms=_latency,
            )
            logger.info("Proactive announcement (%.0fms): %s", _latency, _spoken[:80])
            for _h in self._output_handlers:
                try:
                    await _h(_output)
                except Exception as _e:
                    logger.error("Output handler error: %s", _e)
            return  # ← skip LLM

        # ── 5. Run LLM reasoning in thread pool (non-blocking) ───────────────
        session_id = event.session_id or state.session_id or f"auto-{int(time.time())}"

        try:
            loop    = asyncio.get_running_loop()
            action  = await asyncio.wait_for(
                loop.run_in_executor(
                    self._executor,
                    self._call_reasoning,
                    state.workflow_name,
                    event.screen_context,
                    instruction,
                    session_id,
                ),
                timeout=LLM_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            logger.error("LLM reasoning timed out after %ds", LLM_TIMEOUT_SECS)
            return
        except Exception as exc:
            logger.error("LLM reasoning failed: %s", exc)
            return

        # ── 6. Permission check ──────────────────────────────────────────────
        if action.get("action") == "tool_call" and self._permission:
            tool_name = _resolve_tool(action.get("field_id"), action.get("action"))
            perm = self._permission(tool_name)
            if perm.get("outcome") == "blocked":
                logger.warning("Permission blocked for tool: %s", tool_name)
                action = {"action": "explain",
                          "message": f"Action blocked: {perm.get('reason', 'insufficient permissions')}"}
            elif perm.get("outcome") == "requires_confirmation":
                state.pending_confirm = action
                action = {"action": "confirmation",
                          "message": f"Please confirm: {action.get('reason', 'proceed?')}",
                          "fields_to_confirm": [action.get("field_id")]}

        # ── 7. Build TTS spoken text ─────────────────────────────────────────
        spoken_text = _build_spoken_text(action, vad_result, event.event_type)

        # ── 8. TTS in thread pool (non-blocking) ─────────────────────────────
        tts_path = None
        if self._tts and spoken_text:
            try:
                tts_path = await asyncio.get_running_loop().run_in_executor(
                    self._executor,
                    self._tts,
                    spoken_text,
                )
            except Exception as exc:
                logger.warning("TTS failed: %s", exc)

        # ── 9. Update counters ───────────────────────────────────────────────
        if action.get("action") == "tool_call":
            state.action_count += 1

        # ── 10. Audit log (fire and forget) ──────────────────────────────────
        if self._audit:
            try:
                self._audit(
                    session_id=session_id,
                    user_input=instruction,
                    screen_context=event.screen_context,
                    llm_raw_output="",
                    validated_output=action,
                    tool_executed=action.get("field_id"),
                    result=action.get("action", "unknown"),
                    mode="vision_loop",
                    workflow=state.workflow_name,
                )
            except Exception as exc:
                logger.warning("Audit log failed: %s", exc)

        # ── 11. Route output ─────────────────────────────────────────────────
        latency_ms = round((time.monotonic() - t_start) * 1000, 1)
        output = LoopOutput(
            session_id=session_id,
            action=action,
            vad_result=vad_result,
            spoken_text=spoken_text,
            tts_audio_path=tts_path,
            execute_in_tab=action.get("action") == "tool_call",
            latency_ms=latency_ms,
        )

        logger.info(
            "Loop output — action: %s, latency: %.0fms, spoken: %s",
            action.get("action"), latency_ms, spoken_text[:60],
        )

        for handler in self._output_handlers:
            try:
                await handler(output)
            except Exception as exc:
                logger.error("Output handler error: %s", exc)

    # ── Thread-pool callbacks (blocking, run via executor) ───────────────────

    def _call_reasoning(
        self,
        workflow: str,
        context: dict,
        instruction: str,
        session_id: str,
    ) -> dict:
        """Called in a thread pool — may block up to LLM_TIMEOUT_SECS."""
        return self._reasoning(
            workflow=workflow,
            screen_context=context,
            instruction=instruction,
            session_id=session_id,
        )


# ── Helpers ────────────────────────────────────────────────────────────────

def _context_hash(context: dict, instruction: str) -> str:
    try:
        fields = [
            {"id": f.get("field_id"), "value": f.get("value")}
            for section in context.get("sections", [])
            for f in section.get("fields", [])
        ]
        payload = json.dumps({"fields": fields, "instr": instruction[:50]},
                             sort_keys=True, default=str)
    except Exception:
        payload = str(context) + instruction
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def _resolve_tool(field_id: str | None, action_type: str) -> str:
    if not field_id:
        return "browser.click"
    fid = field_id.lower()
    if "submit" in fid:  return "browser.submit"
    if "click"  in fid:  return "browser.click"
    if "nav"    in fid:  return "browser.navigate"
    return "browser.fill"


def _build_spoken_text(action: dict, vad: VADResult, event_type: EventType) -> str:
    """Build spoken response using Stage 1 voice personality helpers."""
    try:
        from voice.voice_controller import _build_short_response
        return _build_short_response(action, screen_context=None)
    except Exception:
        pass
    # Fallback
    at = action.get("action", "")
    if at == "tool_call":
        import re as _re
        label = action.get("label", "") or ""
        fid   = action.get("field_id", "") or ""
        field = _re.sub(r"^\([^)]*\)\s*", "", label).strip() if label.strip()                 else _re.sub(r"^(enter_|input_|field_)", "", fid, flags=_re.I).replace("_"," ").title()
        return f"Done — I've set {field} to {action.get('value', '')}."
    if at == "explain":      return (action.get("message") or "")[:200]
    if at == "confirmation": return f"Please confirm: {action.get('message','proceed?')}"
    if at == "error":        return f"Sorry: {action.get('reason','unknown error')}"
    if at == "navigation":   return f"Navigating to {action.get('url','the next page')}."
    return ""

def get_vision_loop() -> CopilotVisionLoop | None:
    return _loop_instance


def init_vision_loop(
    reasoning_fn,
    explainer_fn,
    tts_fn=None,
    permission_fn=None,
    audit_fn=None,
) -> CopilotVisionLoop:
    global _loop_instance
    _loop_instance = CopilotVisionLoop(
        reasoning_fn=reasoning_fn,
        explainer_fn=explainer_fn,
        tts_fn=tts_fn,
        permission_fn=permission_fn,
        audit_fn=audit_fn,
    )
    return _loop_instance