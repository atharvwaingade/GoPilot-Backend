"""
ollama_client.py — Chat API with persistent connection and pre-warmed VRAM.

Target: <1s total including network round-trip.
Model: qwen2.5:3b @ 100% GPU = ~600-800ms inference on RTX 3050 6GB.
"""
import logging
import os
import threading
import requests

logger = logging.getLogger(__name__)

# Allow overriding the Ollama server URL and model via environment variables
# or via config.py Pydantic settings (COPILOT_OLLAMA_URL / COPILOT_OLLAMA_MODEL).
# Falls back to the hard-coded defaults so existing deployments are unaffected.
try:
    from config import settings as _settings
    _OLLAMA_BASE_URL = _settings.ollama_url.rstrip("/")
    MODEL            = _settings.ollama_model
except Exception:
    _OLLAMA_BASE_URL = os.environ.get("COPILOT_OLLAMA_URL", "http://localhost:11434").rstrip("/")
    MODEL            = os.environ.get("COPILOT_OLLAMA_MODEL", "qwen2.5:3b")

OLLAMA_CHAT_URL = f"{_OLLAMA_BASE_URL}/api/chat"

NUM_CTX         = 2048
NUM_PREDICT     = 300   # single action ~80 tokens; multi-fill array ~200 tokens
TEMPERATURE     = 0.0
TIMEOUT         = 10

SYSTEM = (
    "You output ONLY a single JSON object. No text before or after.\n"
    "You will receive:\n"
    "  MAP: a label→field_id dictionary\n"
    "  CMD: the user instruction\n"
    "  OUT: (optional) the expected JSON — output it as-is if provided\n"
    "For fill/set/enter tasks output EXACTLY:\n"
    '{"action":"tool_call","field_id":"<id from MAP>","value":"<value>","reason":""}\n'
    "The field_id MUST be copied exactly from the MAP values (right side), not the keys.\n"
    "Action must be one of: tool_call, explain, confirmation, error."
)

_lock    = threading.Lock()
_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    with _lock:
        if _session is None:
            _session = requests.Session()
            # Pre-warm: keep model hot in VRAM
            try:
                _session.post(OLLAMA_CHAT_URL, json={
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user",   "content": 'MAP:{"name":"name"}\nCMD:fill name as test\nOUT:{"action":"tool_call","field_id":"name","value":"test","reason":""}'},
                    ],
                    "stream": False, "format": "json",
                    "options": {"num_predict": 20, "num_ctx": NUM_CTX},
                }, timeout=60)
                logger.info("Ollama pre-warmed — %s in VRAM", MODEL)
            except Exception as exc:
                logger.warning("Pre-warm failed: %s", exc)
        return _session


class OllamaUnavailableError(Exception): pass
class OllamaResponseError(Exception):    pass


def generate(prompt: str) -> str:
    if len(prompt) > NUM_CTX * 3:
        prompt = prompt[:NUM_CTX * 3]

    try:
        resp = _get_session().post(OLLAMA_CHAT_URL, json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": TEMPERATURE, "num_predict": NUM_PREDICT, "num_ctx": NUM_CTX},
        }, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError as e:
        raise OllamaUnavailableError("Ollama not running — run: ollama serve") from e
    except requests.exceptions.Timeout:
        raise OllamaUnavailableError(f"Timed out after {TIMEOUT}s")
    except requests.exceptions.HTTPError as e:
        raise OllamaResponseError(f"HTTP {resp.status_code}") from e

    data = resp.json()
    if "error" in data:
        raise OllamaResponseError(data["error"])

    text = data.get("message", {}).get("content") or data.get("response", "")
    if not text:
        raise OllamaResponseError("Empty response")

    logger.debug("LLM: %s", text[:200])
    return text