# GoPilot Backend

GoPilot is a **real-time ERP/web form copilot backend** built with FastAPI.
It combines:

- DOM/screen-context understanding
- LLM-driven workflow reasoning (via local Ollama)
- Voice input/output (Whisper STT + local TTS)
- Browser action safety/permission gating
- Real-time WebSocket event loop for extension-driven automation

The repo also contains a Chrome extension (`backend/extension/`) that connects to this backend.

## Repository Structure

```text
GoPilot-Backend/
└── backend/
    ├── main.py                  # FastAPI app + all API routes
    ├── config.py                # Environment-based settings
    ├── requirements.txt         # Python dependencies
    ├── core/                    # Engine startup + hardware detection
    ├── model_manager/           # Ollama client + mode selection
    ├── reasoning_layer/         # Prompting, validation, action schemas
    ├── workflow_core/           # Purchase/Supplier/Sell/Free workflows
    ├── security/                # Tool risk policy and permission guard
    ├── logs/                    # Audit logger + replay schema
    ├── vision_core/             # Vision event loop + websocket bridge
    ├── voice/                   # STT, TTS, memory, recovery, readback
    ├── screen_context/          # Screen/DOM context parser and schema
    ├── plugins/                 # Plugin registry and built-in handlers
    └── extension/               # Chrome extension (popup/content/background)
```

## Key Features

- **Workflow-aware AI actions** with fallback to `free` mode for unknown pages
- **Autonomous multi-step execution** with stop conditions and audit trail
- **Tool permission policy** (`low/medium/high` risk outcomes)
- **Voice pipeline**: audio upload -> transcription -> AI action -> spoken response
- **Proactive announcements** on navigation and toggle events
- **Page memory** to resume interrupted form-filling across page changes
- **WebSocket control loop** for near real-time extension/backend communication

## Tech Stack

- Python 3.11+
- FastAPI + Uvicorn
- Pydantic v2
- Ollama (local LLM backend)
- Whisper (`openai-whisper`) for STT
- Local TTS backends (Kokoro recommended)
- Chrome Extension (Manifest V3)

## Prerequisites

1. **Python 3.11+**
2. **Ollama** installed and running locally
3. (Optional but recommended) A CUDA-capable GPU for faster inference
4. (Voice features) Audio dependencies for STT/TTS

## Quick Start

### 1) Clone and enter backend

```bash
cd /path/to/GoPilot-Backend/backend
```

### 2) Create virtual environment

```bash
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate   # Windows PowerShell
```

### 3) Install Python dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4) Set up Ollama

Start Ollama and pull the model used by this codebase (`qwen2.5:3b`):

```bash
ollama serve
ollama pull qwen2.5:3b
```

### 5) (Optional) Install recommended TTS backend

Kokoro is the default backend in code:

```bash
pip install kokoro soundfile
# Windows may also need:
pip install pywin32
```

### 6) Run the API server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open API docs:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Configuration

Settings are loaded from `.env` in `backend/` with `COPILOT_` prefix.

### Core settings (`backend/config.py`)

| Variable | Default | Description |
|---|---|---|
| `COPILOT_APP_NAME` | `CoPilot Platform` | Application name |
| `COPILOT_APP_VERSION` | `0.1.0` | Application version |
| `COPILOT_DEBUG` | `false` | Debug mode |
| `COPILOT_LOG_LEVEL` | `INFO` | Log level |
| `COPILOT_HOST` | `0.0.0.0` | Bind host |
| `COPILOT_PORT` | `8000` | Bind port |
| `COPILOT_GPU_VRAM_THRESHOLD_GB` | `4.0` | GPU threshold signal |
| `COPILOT_AUDIT_LOGS_DIR` | `logs/sessions` | Audit session log directory |

### Voice settings (from `voice/` modules)

| Variable | Default | Description |
|---|---|---|
| `COPILOT_TTS_BACKEND` | `kokoro` | TTS backend: `kokoro`, `pyttsx3`, `parler` |
| `COPILOT_TTS_VOICE_DESC` | preset text | Voice description (mainly for Parler) |
| `COPILOT_STT_LANG` | `auto` | Whisper language (`auto`, `en`, `hi`, etc.) |
| `COPILOT_WHISPER_MODEL` | `small` | Whisper model size |

### Example `.env`

```env
COPILOT_LOG_LEVEL=INFO
COPILOT_HOST=0.0.0.0
COPILOT_PORT=8000
COPILOT_TTS_BACKEND=kokoro
COPILOT_STT_LANG=auto
COPILOT_WHISPER_MODEL=small
```

## Chrome Extension Integration

Extension source is in `backend/extension`.

### Load unpacked extension (Chrome)

1. Open `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked**
4. Select: `.../GoPilot-Backend/backend/extension`

The extension expects backend endpoints at:

- HTTP: `http://localhost:8000`
- WS: `ws://localhost:8000/ws/vision`

## API Overview

### Screen + LLM

- `POST /screen/parse` — parse DOM-like payload to structured screen context
- `POST /llm/test` — test prompt against local Ollama

### Plugins

- `GET /plugins`
- `POST /plugins/{plugin_name}/enable`
- `POST /plugins/{plugin_name}/disable`

### Workflow + Execution

- `POST /workflow/{workflow_name}/ai_step`
- `POST /workflow/{workflow_name}/autorun`

Supported registered workflows: `purchase`, `supplier`, `sell`, `free`.

### Security

- `POST /tools/check` — permission decision for tool execution
- `GET /tools/registry` — list tool risk registry

### Audit

- `GET /logs/replay/{session_id}`
- `GET /logs/sessions`

### Voice

- `POST /voice/process`
- `GET /voice/audio/{filename}`
- `POST /voice/stream`
- `POST /voice/toggle`
- `POST /voice/announce`
- `POST /voice/page_memory/save`
- `POST /voice/page_memory/clear`
- `GET /voice/page_memory/status/{tab_session_id}`
- `POST /voice/recover`
- `POST /voice/readback`

### Vision

- `WebSocket /ws/vision`
- `POST /vision/explain`
- `POST /vision/navigate`

## WebSocket Message Protocol (`/ws/vision`)

Incoming message types include:

- `dom_snapshot`
- `navigation`
- `voice_command`
- `user_text`
- `confirmation`
- `ping`

Outgoing message types include:

- `action`
- `tts_ready` / `tts_chunk`
- `vad_status`
- `ack`
- `error`
- `pong`

## Logging and Artifacts

- Session audit logs are stored under `backend/logs/sessions/`.
- Generated voice files are stored under `backend/voice_output/`.

## Development Notes

- No test suite is currently checked into this repository.
- API docs (`/docs`) are the fastest way to inspect request/response schemas.
- The backend pre-warms STT/TTS on startup to reduce first-request latency.

## Troubleshooting

### Ollama errors (`503` / unavailable)

- Ensure Ollama daemon is running: `ollama serve`
- Ensure model exists: `ollama pull qwen2.5:3b`

### Voice transcription fails

- Verify `openai-whisper` is installed
- For non-WAV audio formats, ensure decode dependencies are available

### No TTS audio generated

- Confirm selected backend via `COPILOT_TTS_BACKEND`
- Install missing backend deps (e.g., Kokoro)
- Check server logs for fallback-to-silent warnings

### Extension connects but no actions happen

- Confirm backend is at `http://localhost:8000`
- Confirm WebSocket endpoint is reachable at `ws://localhost:8000/ws/vision`
- Check browser extension service worker and content-script logs

## Security Model (Current)

Tool executions are gated by risk policy:

- `low` -> auto execute
- `medium` -> log and execute
- `high` -> requires confirmation
- unknown tool -> blocked

Registered examples include browser fill/click/submit and system open app.

## License

No license file is currently present in this repository. Add one if you plan to distribute the project publicly.
