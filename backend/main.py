import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from vision_core.event_loop import init_vision_loop, CopilotVisionLoop
from vision_core.ws_server import handle_websocket, manager as ws_manager
from vision_core.page_explainer import explain_page
from fastapi import WebSocket

from config import settings
from core.engine import configure_logging, engine
from model_manager.mode_selector import OperationMode, select_mode
from model_manager.ollama_client import (
    OllamaUnavailableError,
    OllamaResponseError,
    generate,
)

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Starting %s v%s", settings.app_name, settings.app_version)
    engine.startup()

    # ── Pre-warm TTS + Whisper in background (eliminates first-call delay) ───
    import threading
    def _prewarm():
        try:
            logger.info("Pre-warming TTS (Kokoro)...")
            from voice.tts_engine import tts_engine
            tts_engine.synthesise("Ready.", play=False)
            logger.info("TTS pre-warm complete")
        except Exception as e:
            logger.warning("TTS pre-warm failed: %s", e)
        try:
            logger.info("Pre-warming Whisper STT...")
            from voice.stt_engine import _load_model
            _load_model()
            logger.info("Whisper pre-warm complete")
        except Exception as e:
            logger.warning("Whisper pre-warm failed: %s", e)
    threading.Thread(target=_prewarm, daemon=True).start()

    # ── Wire up CoPilot Vision Loop ──────────────────────────────────────────
    from reasoning_layer.controller import ReasoningController
    from security.permissions import tool_permission_guard
    from logs.audit_logger import audit_logger
    from model_manager.mode_selector import select_mode
    from vision_core.page_explainer import explain_page
    from model_manager.ollama_client import generate

    hw   = engine.hardware
    mode = select_mode(hw)
    rc   = ReasoningController(mode=mode)

    def _reasoning_fn(workflow, screen_context, instruction, session_id):
        from workflow_core.registry import workflow_registry
        wf        = workflow_registry.get_best(workflow, screen_context)
        wf_result = wf.next_step(screen_context)
        missing   = [
            f["field_id"]
            for section in screen_context.get("sections", [])
            for f in section.get("fields", [])
            if f.get("field_id") in wf.required_fields
            and not (f.get("value") or "")
        ]
        action = rc.run(
            workflow_name=workflow,
            screen_context=screen_context,
            user_instruction=instruction,
            next_field=wf_result.next_field,
            calculated_fields=wf.calculated_fields,
            required_fields=wf.required_fields,
            missing_required=missing,
        )
        # run() can return a plain dict (multi_fill path) or a Pydantic model
        return action if isinstance(action, dict) else action.model_dump()

    def _explainer_fn(context, instruction):
        return explain_page(context, instruction, generate)

    def _permission_fn(tool_name):
        result = tool_permission_guard.check(tool_name)
        return {"outcome": result.outcome.value, "reason": result.reason}

    # Try to load TTS (optional — voice disabled if not installed)
    try:
        from voice.tts_engine import tts_engine
        _tts_fn = tts_engine.synthesise
    except Exception:
        _tts_fn = None
        logger.warning("TTS not available — vision loop will not speak")

    vision_loop = init_vision_loop(
        reasoning_fn=_reasoning_fn,
        explainer_fn=_explainer_fn,
        tts_fn=_tts_fn,
        permission_fn=_permission_fn,
        audit_fn=audit_logger.log,
    )
    await vision_loop.start()
    logger.info("CoPilot Vision loop started")
    # ── End Vision Loop setup ─────────────────────────────────────────────────

    yield

    await vision_loop.stop()
    engine.shutdown()

app = FastAPI(lifespan=lifespan)

# ── Screen context ─────────────────────────────────────────────────────────

from screen_context.parser import ScreenContextSchema, screen_context_parser
from pydantic import ValidationError


@app.post("/screen/parse", response_model=ScreenContextSchema, tags=["screen"])
async def screen_parse(body: dict) -> ScreenContextSchema:
    """Parse a simplified DOM-like JSON into a structured, token-safe ScreenContextSchema."""
    try:
        return screen_context_parser.parse(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── LLM ───────────────────────────────────────────────────────────────────


class LLMRequest(BaseModel):
    prompt: str


class LLMResponse(BaseModel):
    response: str


@app.post("/llm/test", response_model=LLMResponse, tags=["llm"])
async def llm_test(body: LLMRequest) -> LLMResponse:
    """Send a prompt to the local Ollama instance and return the raw response."""
    try:
        text = generate(body.prompt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OllamaUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except OllamaResponseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return LLMResponse(response=text)

# ── Plugins ────────────────────────────────────────────────────────────────

from plugins.registry import plugin_registry, PluginCategory


@app.get("/plugins", tags=["plugins"])
async def list_plugins(category: PluginCategory | None = None) -> list[dict]:
    """List all registered plugins, optionally filtered by category."""
    if category:
        return plugin_registry.list_by_category(category)
    return plugin_registry.list_plugins()


@app.post("/plugins/{plugin_name}/enable", tags=["plugins"])
async def enable_plugin(plugin_name: str) -> dict:
    try:
        plugin_registry.enable(plugin_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_name}' not found")
    return {"plugin": plugin_name, "enabled": True}


@app.post("/plugins/{plugin_name}/disable", tags=["plugins"])
async def disable_plugin(plugin_name: str) -> dict:
    try:
        plugin_registry.disable(plugin_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_name}' not found")
    return {"plugin": plugin_name, "enabled": False}

# ── AI Workflow Step ───────────────────────────────────────────────────────

from workflow_core.registry import workflow_registry
from reasoning_layer.controller import ReasoningController
from reasoning_layer.schemas import LLMAction, ErrorAction
from model_manager.mode_selector import select_mode


class AIStepRequest(BaseModel):
    screen_context: dict
    user_instruction: str


@app.post("/workflow/{workflow_name}/ai_step", tags=["workflow"])
async def workflow_ai_step(workflow_name: str, body: AIStepRequest) -> dict:
    """
    Run one LLM reasoning cycle for the named workflow.
    Builds a planning prompt, calls Ollama, validates output,
    retries once on failure, returns ErrorAction if unrecoverable.
    """
    try:
        workflow = workflow_registry.get(workflow_name)  # validated name exists
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow '{workflow_name}' not found. "
                   f"Available: {workflow_registry.list_workflows()}",
        )

    # Use best workflow for this page (falls back to FreeWorkflow if field IDs don't match)
    workflow = workflow_registry.get_best(workflow_name, body.screen_context)
    result = workflow.next_step(body.screen_context)

    # Collect field metadata from workflow
    missing_required = []
    for section in body.screen_context.get("sections", []):
        for field in section.get("fields", []):
            fid = field.get("field_id", "")
            val = field.get("value")
            if fid in workflow.required_fields and (val is None or str(val).strip() == ""):
                missing_required.append(fid)

    hw = engine.hardware
    mode = select_mode(hw)
    controller = ReasoningController(mode=mode)

    action = controller.run(
        workflow_name=workflow_name,
        screen_context=body.screen_context,
        user_instruction=body.user_instruction,
        next_field=result.next_field,
        calculated_fields=workflow.calculated_fields,
        required_fields=workflow.required_fields,
        missing_required=missing_required,
    )

    return action.model_dump()


# ── Tool permission check ──────────────────────────────────────────────────

from security.permissions import (
    PermissionOutcome,
    PermissionResult,
    tool_permission_guard,
)
from pydantic import BaseModel as _BaseModel


class ToolExecuteRequest(_BaseModel):
    tool_name: str
    payload: dict = {}


class ToolPermissionResponse(_BaseModel):
    tool_name: str
    outcome: PermissionOutcome
    risk_level: str | None
    reason: str
    executed: bool


@app.post("/tools/check", response_model=ToolPermissionResponse, tags=["security"])
async def tool_permission_check(body: ToolExecuteRequest) -> ToolPermissionResponse:
    """
    Evaluate and enforce tool-level permission policy.

    - LOW risk     → auto_execute: proceeds immediately
    - MEDIUM risk  → log_and_execute: logged then allowed
    - HIGH risk    → requires_confirmation: blocked until user confirms
    - Unknown tool → blocked unconditionally
    """
    result: PermissionResult = tool_permission_guard.check(body.tool_name)

    if result.outcome == PermissionOutcome.BLOCKED:
        raise HTTPException(status_code=403, detail=result.reason)

    if result.outcome == PermissionOutcome.REQUIRES_CONFIRMATION:
        # Return 202 Accepted — caller must POST /tools/confirm to proceed
        return ToolPermissionResponse(
            tool_name=result.tool_name,
            outcome=result.outcome,
            risk_level=result.risk_level.value if result.risk_level else None,
            reason=result.reason,
            executed=False,
        )

    # LOW or MEDIUM — cleared to execute
    return ToolPermissionResponse(
        tool_name=result.tool_name,
        outcome=result.outcome,
        risk_level=result.risk_level.value if result.risk_level else None,
        reason=result.reason,
        executed=True,
    )


@app.get("/tools/registry", tags=["security"])
async def list_tool_registry() -> list[dict]:
    """Return all registered tools and their risk levels."""
    from security.permissions import TOOL_REGISTRY
    return [
        {"tool_name": name, "risk_level": level.value}
        for name, level in sorted(TOOL_REGISTRY.items())
    ]


# ── Audit log replay ───────────────────────────────────────────────────────

from logs.audit_logger import audit_logger
from logs.replay_schema import AuditEntry, ReplayResponse


@app.get("/logs/replay/{session_id}", response_model=ReplayResponse, tags=["audit"])
async def replay_session(session_id: str) -> ReplayResponse:
    """
    Return the full ordered audit trace for a session.
    Each entry contains the complete action record written at execution time.
    """
    try:
        entries = audit_logger.replay(session_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"No audit log found for session '{session_id}'",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    validated = [AuditEntry.model_validate(e) for e in entries]

    return ReplayResponse(
        session_id=session_id,
        total_entries=len(validated),
        entries=validated,
    )


@app.get("/logs/sessions", tags=["audit"])
async def list_audit_sessions() -> dict:
    """Return all session IDs that have audit logs on disk."""
    return {"sessions": audit_logger.list_sessions()}

# ── Autonomous execution ───────────────────────────────────────────────────

from core.executor import AutonomousExecutor, StopReason
from reasoning_layer.controller import ReasoningController
from security.permissions import tool_permission_guard
from logs.audit_logger import audit_logger


class AutoRunRequest(BaseModel):
    session_id: str
    screen_context: dict
    user_instruction: str


class StepSummary(BaseModel):
    step: int
    action_type: str
    field_id: str | None
    value: object = None
    permission_outcome: str | None
    result: str
    reflection_retries: int


class AutoRunResponse(BaseModel):
    session_id: str
    workflow: str
    mode: str
    stop_reason: str
    total_steps: int
    final_action: dict | None
    errors: list[str]
    steps: list[StepSummary]


@app.post(
    "/workflow/{workflow_name}/autorun",
    response_model=AutoRunResponse,
    tags=["workflow"],
)
def workflow_autorun(workflow_name: str, body: AutoRunRequest) -> AutoRunResponse:
    """
    Run the autonomous execution controller for up to 10 deterministic steps.

    Halts on: workflow complete, error action, permission denied,
    confirmation required, infinite loop, or max steps reached.
    """
    try:
        workflow = workflow_registry.get(workflow_name)  # validated name exists
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow '{workflow_name}' not found. "
                   f"Available: {workflow_registry.list_workflows()}",
        )

    hw         = engine.hardware
    mode       = select_mode(hw)
    controller = ReasoningController(mode=mode)

    executor = AutonomousExecutor(
        reasoning_controller=controller,
        permission_guard=tool_permission_guard,
        workflow=workflow,
        audit_logger=audit_logger,
    )

    execution = executor.run(
        session_id=body.session_id,
        workflow_name=workflow_name,
        screen_context=body.screen_context,
        user_instruction=body.user_instruction,
        mode_value=mode.value,
    )

    raw = execution.to_dict()

    return AutoRunResponse(
        session_id=raw["session_id"],
        workflow=raw["workflow"],
        mode=raw["mode"],
        stop_reason=raw["stop_reason"],
        total_steps=raw["total_steps"],
        final_action=raw["final_action"],
        errors=raw["errors"],
        steps=[StepSummary(**s) for s in raw["steps"]],
    )

# ── Voice ──────────────────────────────────────────────────────────────────

import os
from fastapi import UploadFile, File, Form
from fastapi.responses import FileResponse
from voice.voice_controller import get_voice_controller


class VoiceResponse(BaseModel):
    transcription:          str
    normalised:             str  = ""    # after multilingual normalisation
    ai_response:            str
    audio_file:             str
    action:                 dict
    detail_mode:            bool
    submit_guard_triggered: bool
    guided_fill_active:     bool = False
    nav_action:             bool = False  # True = popup must execute as navigate/click
    warning:                str | None
    has_memory:             bool = False
    multi_fill_active:      bool = False


@app.post("/voice/process", response_model=VoiceResponse, tags=["voice"])
def voice_process(
    audio: UploadFile = File(..., description="WAV audio file from client"),
    workflow: str             = Form(default="purchase"),
    session_id: str           = Form(default=""),
    tab_session_id: str       = Form(default=""),
    screen_context: str       = Form(default="{}"),
    play_audio: bool          = Form(default=False),
    multi_fill_trigger: bool  = Form(default=False),
) -> VoiceResponse:
    """
    Full voice pipeline: Audio → STT → AI step → TTS → WAV.

    Flow:
      1. Read uploaded audio bytes
      2. Whisper small (CPU) transcribes speech to text
      3. AI step called with transcription as instruction
      4. Submit guard prevents auto-submit from first voice command
      5. Adaptive TTS response (short/detailed based on context)
      6. Returns transcription, AI action, and path to WAV response

    Send as multipart/form-data:
      audio         = WAV file
      workflow      = purchase | supplier | sell
      session_id    = string
      screen_context = JSON string of current page context
    """
    import json as _json

    # Parse screen_context from JSON string form field
    try:
        context_dict = _json.loads(screen_context) if screen_context else {}
    except _json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="screen_context is not valid JSON")

    if not session_id:
        import time
        session_id = f"voice-{int(time.time())}"

    # Read audio bytes
    audio_bytes = audio.file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Audio file is empty")

    suffix = os.path.splitext(audio.filename or ".wav")[1] or ".wav"

    # Validate workflow
    try:
        from workflow_core.registry import workflow_registry
        workflow_obj = workflow_registry.get(workflow)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow '{workflow}' not found. Available: {workflow_registry.list_workflows()}",
        )

    # Build the AI step callable for the voice controller
    from reasoning_layer.controller import ReasoningController
    from model_manager.mode_selector import select_mode

    hw   = engine.hardware
    mode = select_mode(hw)
    rc   = ReasoningController(mode=mode)

    def ai_step_fn(workflow: str, screen_context: dict, instruction: str, session_id: str) -> dict:
        _wf             = workflow_registry.get_best(workflow, screen_context)
        wf_result       = _wf.next_step(screen_context)
        missing_required = [
            f["field_id"]
            for section in screen_context.get("sections", [])
            for f in section.get("fields", [])
            if f.get("field_id") in _wf.required_fields
            and not (f.get("value") or "")
        ]
        action = rc.run(
            workflow_name=workflow,
            screen_context=screen_context,
            user_instruction=instruction,
            next_field=wf_result.next_field,
            calculated_fields=_wf.calculated_fields,
            required_fields=_wf.required_fields,
            missing_required=missing_required,
        )
        # run() can return a plain dict (multi_fill path) or a Pydantic model
        return action if isinstance(action, dict) else action.model_dump()

    # Run voice pipeline
    try:
        controller = get_voice_controller()
        result     = controller.process(
            audio_bytes=audio_bytes,
            workflow_name=workflow,
            screen_context=context_dict,
            session_id=session_id,
            ai_step_fn=ai_step_fn,
            play_audio=play_audio,
            audio_suffix=suffix,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Log instruction to page memory (tracks what user was doing for cross-page resume)
    _tab_sid = tab_session_id or session_id
    if _tab_sid and result.transcription:
        try:
            from voice.page_memory import page_memory_store
            page_memory_store.log_instruction(_tab_sid, result.transcription)
        except Exception as _pm_err:
            logger.debug("page_memory log_instruction failed: %s", _pm_err)

    return VoiceResponse(
        transcription          = result.transcription,
        normalised             = result.normalised,
        ai_response            = result.spoken_response,
        audio_file             = result.audio_file,
        action                 = result.ai_action,
        nav_action             = getattr(result, "nav_action", False),
        multi_fill_active      = getattr(result, "multi_fill_active", False),
        detail_mode            = result.detail_mode,
        submit_guard_triggered = result.submit_guard_triggered,
        guided_fill_active     = result.guided_fill_active,
        warning                = result.warning,
    )


@app.get("/voice/audio/{filename}", tags=["voice"])
def voice_audio(filename: str) -> FileResponse:
    """
    Download a generated TTS WAV file by filename.
    The audio_file field from /voice/process contains the full path;
    pass only the filename (basename) to this endpoint.
    """
    from voice.tts_engine import AUDIO_OUT_DIR

    # Sanitise — no path traversal
    safe = os.path.basename(filename)
    path = AUDIO_OUT_DIR / safe

    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Audio file '{safe}' not found")

    return FileResponse(
        path=str(path),
        media_type="audio/wav",
        filename=safe,
    )


# ── Text command endpoint ─────────────────────────────────────────────────────
class VoiceTextRequest(BaseModel):
    text:           str
    workflow:       str  = "free"
    session_id:     str  = ""
    screen_context: dict = {}


@app.post("/voice/text", response_model=VoiceResponse, tags=["voice"])
def voice_text(body: VoiceTextRequest) -> VoiceResponse:
    """
    Text command pipeline: Text → AI step → TTS → WAV.

    Identical to /voice/process but skips STT — accepts typed text directly.
    Used by the popup text input and any API consumer that already has text.

    Send as JSON:
      text           = the user's instruction (e.g. "Set category to chairs")
      workflow       = purchase | supplier | sell | free
      session_id     = string
      screen_context = current page DOM context dict
    """
    if not body.text or not body.text.strip():
        raise HTTPException(status_code=400, detail="text is required")

    import time as _time_mod
    session_id = body.session_id or f"text-{int(_time_mod.time())}"

    from workflow_core.registry import workflow_registry
    from reasoning_layer.controller import ReasoningController
    from model_manager.mode_selector import select_mode
    from voice.voice_controller import get_voice_controller

    try:
        workflow_registry.get(body.workflow)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow '{body.workflow}' not found.",
        )

    hw   = engine.hardware
    mode = select_mode(hw)
    rc   = ReasoningController(mode=mode)

    def ai_step_fn(workflow: str, screen_context: dict, instruction: str, session_id: str) -> dict:
        _wf             = workflow_registry.get_best(workflow, screen_context)
        wf_result       = _wf.next_step(screen_context)
        missing_required = [
            f["field_id"]
            for section in screen_context.get("sections", [])
            for f in section.get("fields", [])
            if f.get("field_id") in _wf.required_fields
            and not (f.get("value") or "")
        ]
        action = rc.run(
            workflow_name=workflow,
            screen_context=screen_context,
            user_instruction=instruction,
            next_field=wf_result.next_field,
            calculated_fields=_wf.calculated_fields,
            required_fields=_wf.required_fields,
            missing_required=missing_required,
        )
        return action if isinstance(action, dict) else action.model_dump()

    try:
        controller = get_voice_controller()
        result = controller.process_text(
            text=body.text.strip(),
            workflow_name=body.workflow,
            screen_context=body.screen_context,
            session_id=session_id,
            ai_step_fn=ai_step_fn,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return VoiceResponse(
        transcription          = result.transcription,
        normalised             = result.normalised,
        ai_response            = result.spoken_response,
        audio_file             = result.audio_file,
        action                 = result.ai_action,
        nav_action             = getattr(result, "nav_action", False),
        multi_fill_active      = getattr(result, "multi_fill_active", False),
        detail_mode            = result.detail_mode,
        submit_guard_triggered = result.submit_guard_triggered,
        guided_fill_active     = result.guided_fill_active,
        warning                = result.warning,
    )


class StreamRequest(BaseModel):
    text:       str
    session_id: str = ""


class StreamResponse(BaseModel):
    chunks:  list[str]   # list of /voice/audio/{filename} URLs
    spoken:  str


@app.post("/voice/stream", response_model=StreamResponse, tags=["voice"])
def voice_stream(body: StreamRequest) -> StreamResponse:
    """
    Streaming TTS — splits text into sentences, synthesises each one, and
    returns an ordered list of WAV file URLs. The client plays them in
    sequence so the first sentence starts playing within ~300ms.

    Send as JSON:
      text       = full response text to synthesise
      session_id = optional session identifier

    Returns:
      chunks = list of /voice/audio/<filename> URLs (play in order)
      spoken = the full text as synthesised
    """
    from voice.streaming_tts import get_streaming_tts

    if not body.text or not body.text.strip():
        raise HTTPException(status_code=400, detail="text is required")

    st = get_streaming_tts()
    paths: list[str] = st.speak_streaming(body.text, on_chunk=lambda _: None)

    urls = [f"/voice/audio/{p.replace(chr(92), '/').split('/')[-1]}" for p in paths]
    return StreamResponse(chunks=urls, spoken=body.text)


# ──  WebSocket endpoint ─────────────────────────────────────

@app.websocket("/ws/vision")
async def vision_ws(ws: WebSocket):
    """
    Real-time WebSocket endpoint for CoPilot Vision.

    Chrome extension connects here and sends:
      - dom_snapshot:  periodic DOM state (every 500ms or on MutationObserver)
      - navigation:    URL change detected
      - voice_command: transcribed user speech
      - user_text:     typed command from Tauri app
      - confirmation:  user confirmed/cancelled a modal

    Backend sends:
      - action:      fill/click/navigate instruction for the extension executor
      - tts_ready:   URL to fetch the TTS audio WAV
      - vad_status:  whether the last frame was interesting (for UI feedback)
    """
    await handle_websocket(ws)

# =========================
# Explain Endpoint
# =========================
class ExplainRequest(BaseModel):
    screen_context: dict
    user_instruction: str = "Explain this page"
    session_id: str = ""


class ExplainResponse(BaseModel):
    page_title: str
    page_url: str
    page_purpose: str
    total_fields: int
    filled_fields: int
    completion_pct: float
    required_missing: list[str]
    navigable_actions: list[str]
    explanation: str   # voice-ready


@app.post("/vision/explain", tags=["vision"])
def vision_explain(body: ExplainRequest) -> ExplainResponse:
    """
    Explain the current page in natural language.
    Returns a voice-ready explanation + structured field insights.
    """
    from model_manager.ollama_client import generate
    from vision_core.page_explainer import explain_page

    try:
        insight = explain_page(body.screen_context, body.user_instruction, generate)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return ExplainResponse(
        page_title=insight.page_title,
        page_url=insight.page_url,
        page_purpose=insight.page_purpose,
        total_fields=insight.total_fields,
        filled_fields=insight.filled_fields,
        completion_pct=insight.completion_pct,
        required_missing=insight.required_missing,
        navigable_actions=insight.navigable_actions,
        explanation=insight.raw_explanation,
    )


# =========================
# Navigate Endpoint
# =========================
class NavigateRequest(BaseModel):
    target: str                 # "next page", "submit", "back", or a URL
    screen_context: dict
    session_id: str = ""
    workflow: str = "purchase"


class NavigateResponse(BaseModel):
    action: str                 # "navigate" | "click" | "not_found"
    target_url: str | None
    button_id: str | None
    spoken: str


@app.post("/vision/navigate", tags=["vision"])
def vision_navigate(body: NavigateRequest) -> NavigateResponse:
    """
    Resolve a natural language navigation intent to a concrete action.
    Finds buttons/links on the current page that match the intent.
    """
    target = body.target.lower()

    # Find matching button
    buttons = body.screen_context.get("buttons", [])
    matched = next(
        (b for b in buttons
         if target in (b.get("label") or "").lower()
         or target in (b.get("button_id") or "").lower()),
        None,
    )

    if matched:
        spoken = f"Clicking {matched.get('label', matched.get('button_id', 'button'))}."
        return NavigateResponse(
            action="click",
            target_url=None,
            button_id=matched.get("button_id"),
            spoken=spoken,
        )

    # Check if it's a URL
    if target.startswith("http"):
        return NavigateResponse(
            action="navigate",
            target_url=body.target,
            button_id=None,
            spoken=f"Navigating to {body.target}.",
        )

    return NavigateResponse(
        action="not_found",
        target_url=None,
        button_id=None,
        spoken=f"I couldn't find a way to navigate to {body.target} on this page.",
    )

# ── Proactive voice endpoints (Stage 2) ────────────────────────────────────

class ToggleRequest(BaseModel):
    enabled:        bool
    screen_context: dict = {}
    session_id:     str  = ""
    url:            str  = ""


class ToggleResponse(BaseModel):
    enabled:    bool
    audio_file: str
    spoken:     str


@app.post("/voice/toggle", response_model=ToggleResponse, tags=["voice"])
def voice_toggle(body: ToggleRequest) -> ToggleResponse:
    """
    Called when the extension's on/off toggle changes.

    ON  → speaks a proactive page announcement: page type, field count, what's missing.
    OFF → speaks "CoPilot is now paused."

    The extension plays the returned audio_file immediately.
    """
    from voice.voice_controller import get_voice_controller
    vc = get_voice_controller()

    if body.enabled:
        audio_file = vc.announce_page(
            screen_context = body.screen_context,
            url            = body.url,
            toggle_on      = True,
        )
        from voice.proactive_announcer import build_page_announcement
        spoken = build_page_announcement(body.screen_context, url=body.url, enabled=True)
    else:
        audio_file = vc.announce_toggle_off()
        from voice.proactive_announcer import build_toggle_off_message
        spoken = build_toggle_off_message()

    return ToggleResponse(enabled=body.enabled, audio_file=audio_file, spoken=spoken)


class AnnounceRequest(BaseModel):
    screen_context:  dict
    session_id:      str = ""
    tab_session_id:  str = ""   # tab-stable ID for cross-page memory
    url:             str = ""


class AnnounceResponse(BaseModel):
    audio_file: str
    spoken:     str


@app.post("/voice/announce", response_model=AnnounceResponse, tags=["voice"])
def voice_announce(body: AnnounceRequest) -> AnnounceResponse:
    """
    Proactive page announcement on navigation (called by vision_observer.js
    when a new page loads and the extension is ON).

    Checks cross-page memory and prepends a resume prompt if the user was
    mid-fill on a previous page.
    """
    from voice.voice_controller import get_voice_controller
    from voice.proactive_announcer import build_page_announcement
    from voice.page_memory import page_memory_store

    vc = get_voice_controller()

    # ── Page announcement (always) ─────────────────────────────────────────
    spoken = build_page_announcement(body.screen_context, url=body.url, enabled=False)

    # ── Resume prompt (if memory exists) ──────────────────────────────────
    tab_sid = body.tab_session_id or body.session_id
    if tab_sid:
        try:
            resume = page_memory_store.get_resume_prompt(
                tab_session_id=tab_sid,
                new_url=body.url,
                new_context=body.screen_context,
            )
            if resume:
                # Prepend resume prompt — user hears memory FIRST, then page info
                spoken = resume + " " + spoken
        except Exception as _me:
            logger.warning("PageMemory resume error: %s", _me)

    audio = vc._tts.synthesise(spoken)
    return AnnounceResponse(audio_file=audio, spoken=spoken)


# ── Page memory endpoints ──────────────────────────────────────────────────────

class PageMemorySaveRequest(BaseModel):
    tab_session_id:    str
    screen_context:    dict
    url:               str  = ""
    last_instruction:  str  = ""
    was_in_guided_fill: bool = False

class PageMemoryClearRequest(BaseModel):
    tab_session_id: str

@app.post("/voice/page_memory/save", tags=["voice"])
def page_memory_save(body: PageMemorySaveRequest):
    """
    Save a snapshot of the page the user was working on BEFORE navigating away.
    Called by vision_observer.js in onNavigate(), before the URL changes.
    """
    from voice.page_memory import page_memory_store
    snap = page_memory_store.save_snapshot(
        tab_session_id    = body.tab_session_id,
        context           = body.screen_context,
        url               = body.url,
        last_instruction  = body.last_instruction,
        was_in_guided_fill= body.was_in_guided_fill,
    )
    return {"saved": snap is not None, "page_type": snap.page_type if snap else None}


@app.post("/voice/page_memory/clear", tags=["voice"])
def page_memory_clear(body: PageMemoryClearRequest):
    """Clear page memory after a successful submit."""
    from voice.page_memory import page_memory_store
    page_memory_store.clear_snapshot(body.tab_session_id)
    return {"cleared": True}


@app.get("/voice/page_memory/status/{tab_session_id}", tags=["voice"])
def page_memory_status(tab_session_id: str):
    """Debug endpoint — inspect what memory exists for a tab session."""
    from voice.page_memory import page_memory_store
    snap = page_memory_store.get_snapshot(tab_session_id)
    if not snap:
        return {"has_memory": False}
    return {
        "has_memory":       True,
        "page_type":        snap.page_type,
        "filled_count":     len(snap.filled_fields),
        "unfilled_required":len(snap.unfilled_required),
        "was_guided_fill":  snap.was_in_guided_fill,
        "age_seconds":      round(__import__("time").monotonic() - snap.timestamp),
        "last_instruction": snap.last_instruction,
    }


# ── Error recovery endpoint ────────────────────────────────────────────────────

class RecoverRequest(BaseModel):
    error_reason:   str
    field_id:       str  = ""
    field_label:    str  = ""
    original_value: str  = ""
    session_id:     str  = ""
    screen_context: dict = {}
    error_source:   str  = "executor"   # "executor" | "dom" | "submit"

class RecoverResponse(BaseModel):
    spoken:           str
    audio_file:       str
    recovery_pending: bool   # True = backend entered recovery mode for next turn
    error_kind:       str

@app.post("/voice/recover", response_model=RecoverResponse, tags=["voice"])
def voice_recover(body: RecoverRequest) -> RecoverResponse:
    """
    Called by popup.js when a fill action fails at the executor level,
    or when DOM validation shows an error after fill.

    Returns Kokoro TTS audio of the error description + fix offer, and
    enters recovery mode so the next /voice/process call routes the
    user's response as a correction answer.
    """
    from voice.voice_controller import get_voice_controller
    from voice.error_recovery import (
        recovery_state, parse_executor_error, parse_dom_error,
        build_recovery_spoken,
    )

    vc  = get_voice_controller()
    sid = body.session_id

    # Parse the error into a recovery object
    if body.error_source == "dom":
        rec = parse_dom_error(
            field_id       = body.field_id,
            field_label    = body.field_label,
            filled_value   = body.original_value,
            dom_error_text = body.error_reason,
        )
        if rec is None:
            spoken = f"There was a validation issue with {body.field_label or 'that field'}."
            audio  = vc._tts.synthesise(spoken)
            return RecoverResponse(spoken=spoken, audio_file=audio,
                                   recovery_pending=False, error_kind="dom_validation")
    else:
        rec = parse_executor_error(
            reason         = body.error_reason,
            field_id       = body.field_id,
            field_label    = body.field_label,
            original_value = body.original_value,
            screen_context = body.screen_context or None,
        )

    spoken = build_recovery_spoken(rec)

    # Enter recovery mode — next voice turn routes as correction answer
    recovery_pending = False
    if sid and rec.recovery is not None:
        recovery_state.set_pending(sid, rec)
        recovery_pending = True

    audio = vc._tts.synthesise(spoken)
    logger.info("Recovery voiced: kind=%s field=%s pending=%s",
                rec.error_kind, rec.field_id, recovery_pending)
    return RecoverResponse(
        spoken=spoken,
        audio_file=audio,
        recovery_pending=recovery_pending,
        error_kind=rec.error_kind,
    )


# ── Readback endpoint ──────────────────────────────────────────────────────────

class ReadbackRequest(BaseModel):
    readback_type:  str          # "fill" | "submit" | "nav"
    action:         dict  = {}   # the action that was executed (for fill)
    toast_text:     str   = ""   # DOM toast message (for submit)
    form_cleared:   bool  = False
    screen_context: dict  = {}   # current page context (for nav + next-field hint)
    url:            str   = ""

class ReadbackResponse(BaseModel):
    spoken:     str
    audio_file: str
    sentiment:  str = "neutral"  # "success" | "error" | "neutral" | "unknown"


@app.post("/voice/readback", response_model=ReadbackResponse, tags=["voice"])
def voice_readback(body: ReadbackRequest) -> ReadbackResponse:
    """
    Generate TTS audio for a post-action readback.

    Called by popup.js after result_scanner.js collects DOM results.
    Produces Kokoro TTS audio for:
      - fill: "Done — Category is set to Chairs. Next: Product Name."
      - submit: "Submitted! Invoice SA000012 created."
      - nav: "Opened Purchase Order. 8 fields, 3 still empty."
    """
    from voice.voice_controller import get_voice_controller
    from voice.result_reader import fill_readback, submit_readback, nav_readback

    vc   = get_voice_controller()
    rtype = body.readback_type

    if rtype == "fill":
        spoken    = fill_readback(body.action, body.screen_context or None)
        sentiment = "neutral"

    elif rtype == "submit":
        spoken    = submit_readback(
            toast_text=body.toast_text or None,
            form_cleared=body.form_cleared,
        )
        sentiment = (
            "success" if any(w in spoken.lower() for w in ("success","submitted","created","saved"))
            else "error" if "problem" in spoken.lower() or "fix" in spoken.lower()
            else "unknown"
        )

    elif rtype == "nav":
        spoken    = nav_readback(body.screen_context, url=body.url)
        sentiment = "neutral"

    else:
        spoken    = "Action completed."
        sentiment = "neutral"

    audio = vc._tts.synthesise(spoken)
    return ReadbackResponse(spoken=spoken, audio_file=audio, sentiment=sentiment)