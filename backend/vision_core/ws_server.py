"""
ws_server.py — WebSocket bridge between Chrome extension and CoPilot Vision loop

Replaces the request/response HTTP endpoints with a persistent WebSocket connection.

Message protocol (JSON):
  Extension → Backend:
    { "type": "dom_snapshot",  "session_id": "...", "context": {...}, "url": "..." }
    { "type": "navigation",    "session_id": "...", "context": {...}, "url": "..." }
    { "type": "voice_command", "session_id": "...", "context": {...}, "text": "..." }
    { "type": "user_text",     "session_id": "...", "context": {...}, "text": "..." }
    { "type": "confirmation",  "session_id": "...", "confirmed": true }
    { "type": "ping" }

  Backend → Extension:
    { "type": "action",       "action": {...}, "execute": true, "spoken": "..." }
    { "type": "tts_ready",    "audio_url": "..." }
    { "type": "vad_status",   "interesting": bool, "score": 0.7, "reason": "..." }
    { "type": "loop_status",  "action_count": 3, "workflow": "purchase" }
    { "type": "pong" }
    { "type": "error",        "message": "..." }
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Set

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from vision_core.event_loop import (
    CopilotEvent,
    EventType,
    LoopOutput,
    get_vision_loop,
)

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages all active WebSocket connections."""

    def __init__(self) -> None:
        self._connections: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        logger.info("WS client connected — total: %d", len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        logger.info("WS client disconnected — total: %d", len(self._connections))

    async def send(self, ws: WebSocket, data: dict) -> None:
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.send_json(data)
            except Exception as exc:
                logger.warning("WS send failed: %s", exc)
                self.disconnect(ws)

    async def broadcast(self, data: dict) -> None:
        dead = set()
        for ws in list(self._connections):
            try:
                await self.send(ws, data)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._connections.discard(ws)


manager = ConnectionManager()


# ── Output handler — called by the vision loop ─────────────────────────────

async def _broadcast_loop_output(output: LoopOutput) -> None:
    """Receives LoopOutput from CopilotVisionLoop and broadcasts to all WS clients."""

    # Main action message
    await manager.broadcast({
        "type":       "action",
        "session_id": output.session_id,
        "action":     output.action,
        "execute":    output.execute_in_tab,
        "spoken":     output.spoken_text,
        "latency_ms": output.latency_ms,
    })

    # TTS audio notification — supports both single file and streaming chunks
    if output.tts_audio_path:
        # Check if the path is a JSON list of chunk paths (streaming TTS)
        tts_val = output.tts_audio_path
        if tts_val.startswith("["):
            import json as _json
            try:
                chunk_paths = _json.loads(tts_val)
                for i, cp in enumerate(chunk_paths):
                    fname = cp.replace("\\", "/").split("/")[-1]
                    await manager.broadcast({
                        "type":       "tts_chunk",          # new: streaming chunk
                        "audio_url":  f"/voice/audio/{fname}",
                        "chunk_idx":  i,
                        "total":      len(chunk_paths),
                        "is_last":    i == len(chunk_paths) - 1,
                    })
            except Exception:
                pass  # fall through to single-file path
        else:
            fname = tts_val.replace("\\", "/").split("/")[-1]
            await manager.broadcast({
                "type":      "tts_ready",
                "audio_url": f"/voice/audio/{fname}",
            })

    # VAD status for UI feedback
    await manager.broadcast({
        "type":        "vad_status",
        "interesting": output.vad_result.interesting,
        "score":       output.vad_result.salience_score,
        "change":      output.vad_result.change_type.value,
        "reason":      output.vad_result.reason,
    })


# ── WebSocket endpoint handler ─────────────────────────────────────────────

async def handle_websocket(websocket: WebSocket) -> None:
    """
    Main WebSocket handler — mount this in main.py:

        @app.websocket("/ws/vision")
        async def vision_ws(ws: WebSocket):
            await handle_websocket(ws)
    """
    await manager.connect(websocket)
    loop = get_vision_loop()

    if loop:
        loop.add_output_handler(_broadcast_loop_output)

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await manager.send(websocket, {"type": "error", "message": "Invalid JSON"})
                continue

            msg_type   = msg.get("type", "")
            session_id = msg.get("session_id", f"ws-{int(time.time())}")

            # ── Ping / keepalive ─────────────────────────────────────────────
            if msg_type == "ping":
                await manager.send(websocket, {"type": "pong"})
                continue

            # ── DOM snapshot / navigation ────────────────────────────────────
            if msg_type in ("dom_snapshot", "navigation"):
                context = msg.get("context", {})
                url     = msg.get("url")

                if not context:
                    await manager.send(websocket, {
                        "type": "error", "message": "Empty context in snapshot"
                    })
                    continue

                event_type = (EventType.NAVIGATION if msg_type == "navigation"
                              else EventType.DOM_SNAPSHOT)

                event = CopilotEvent(
                    event_type=event_type,
                    session_id=session_id,
                    screen_context=context,
                    payload={},
                    url=url,
                )

                if loop:
                    queued = await loop.push_event(event)
                    await manager.send(websocket, {
                        "type":   "ack",
                        "queued": queued,
                        "msg":    "drop-stale: old frame evicted" if not queued else "queued",
                    })

            # ── Voice command ────────────────────────────────────────────────
            elif msg_type == "voice_command":
                context    = msg.get("context", {})
                text       = msg.get("text", "")
                workflow   = msg.get("workflow", "purchase")
                instruction = text

                if loop:
                    loop.reset_for_new_task(session_id, workflow, instruction)
                    event = CopilotEvent(
                        event_type=EventType.VOICE_COMMAND,
                        session_id=session_id,
                        screen_context=context,
                        payload={"text": text, "transcription": text},
                        url=msg.get("url"),
                    )
                    await loop.push_event(event)

                await manager.send(websocket, {
                    "type":        "ack",
                    "transcription": text,
                    "workflow":    workflow,
                })

            # ── User text command ────────────────────────────────────────────
            elif msg_type == "user_text":
                context    = msg.get("context", {})
                text       = msg.get("text", "")
                workflow   = msg.get("workflow", "purchase")

                if loop:
                    loop.reset_for_new_task(session_id, workflow, text)
                    event = CopilotEvent(
                        event_type=EventType.USER_TEXT,
                        session_id=session_id,
                        screen_context=context,
                        payload={"text": text},
                        url=msg.get("url"),
                    )
                    await loop.push_event(event)

                await manager.send(websocket, {"type": "ack", "text": text})

            # ── Confirmation response ────────────────────────────────────────
            elif msg_type == "confirmation":
                confirmed = msg.get("confirmed", False)
                if loop and confirmed and loop._state.pending_confirm:
                    context = msg.get("context", loop._state.prev_context or {})
                    event   = CopilotEvent(
                        event_type=EventType.CONFIRMATION,
                        session_id=session_id,
                        screen_context=context,
                        payload={"confirmed": True,
                                 "action": loop._state.pending_confirm},
                    )
                    loop._state.pending_confirm = None
                    await loop.push_event(event)

                await manager.send(websocket, {
                    "type": "ack",
                    "confirmed": confirmed,
                })

            else:
                await manager.send(websocket, {
                    "type":    "error",
                    "message": f"Unknown message type: {msg_type}",
                })

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as exc:
        logger.error("WS handler error: %s", exc)
        manager.disconnect(websocket)