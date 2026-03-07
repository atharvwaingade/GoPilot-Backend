"""
stt_engine.py — Speech-to-Text using OpenAI Whisper

KEY FIX (Windows WinError 2):
  Whisper's model.transcribe(path) internally calls ffmpeg via subprocess to
  decode audio. On Windows, if ffmpeg is not on PATH this raises WinError 2.
  Fix: load audio as a numpy float32 array first (using soundfile for WAV),
  then pass the array to model.transcribe() — ffmpeg is never called.

Language handling:
  - Default: auto-detect (handles Hindi, English, Hinglish/code-switching)
  - Override per-call or via COPILOT_STT_LANG env var
  - Setting to "auto" or "" -> Whisper detects language from audio content
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# -- Configuration -------------------------------------------------------------
STT_LANG      = os.environ.get("COPILOT_STT_LANG", "auto")
WHISPER_MODEL = os.environ.get("COPILOT_WHISPER_MODEL", "small")

# Safe temp dir with no spaces in path
_SAFE_TMP_DIR = Path(__file__).parent.parent / "voice_output" / "tmp"
_SAFE_TMP_DIR.mkdir(parents=True, exist_ok=True)

_model = None


def _load_model():
    """Load the Whisper model once and cache globally."""
    global _model
    if _model is not None:
        return _model
    try:
        import whisper
        logger.info("Loading Whisper '%s' model...", WHISPER_MODEL)
        _model = whisper.load_model(WHISPER_MODEL)
        logger.info("Whisper '%s' loaded successfully", WHISPER_MODEL)
        return _model
    except ImportError as exc:
        raise RuntimeError(
            "openai-whisper is not installed.\n"
            "Run: pip install openai-whisper"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to load Whisper '{WHISPER_MODEL}': {exc}") from exc


def _audio_bytes_to_numpy(audio_bytes: bytes, suffix: str):
    """
    Convert raw audio bytes to float32 numpy array at 16 kHz mono.

    This completely bypasses Whisper's internal ffmpeg subprocess call.
    Works on Windows with no ffmpeg on PATH.

    Strategy:
      1. soundfile  -- handles WAV/FLAC natively, zero external deps
      2. pydub      -- handles WebM/MP3/OGG (needs ffmpeg OR pure-python codecs)
      3. raw path   -- last resort, lets Whisper try (may fail without ffmpeg)
    """
    import io
    import numpy as np

    # Method 1: soundfile (WAV/FLAC, no ffmpeg needed at all)
    try:
        import soundfile as sf
        audio, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32", always_2d=False)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)          # stereo -> mono
        if sr != 16000:
            ratio   = 16000 / sr
            new_len = int(len(audio) * ratio)
            indices = np.linspace(0, len(audio) - 1, new_len)
            audio   = np.interp(indices, np.arange(len(audio)), audio)
        logger.debug("Audio decoded via soundfile: %d samples @ 16kHz", len(audio))
        return audio.astype(np.float32)
    except Exception as _e:
        logger.debug("soundfile failed (%s), trying pydub", _e)

    # Method 2: pydub (WebM/MP3/OGG)
    try:
        from pydub import AudioSegment
        seg  = AudioSegment.from_file(
            io.BytesIO(audio_bytes),
            format=suffix.lstrip(".") or "wav",
        )
        seg  = seg.set_frame_rate(16000).set_channels(1)
        arr  = np.array(seg.get_array_of_samples(), dtype=np.float32)
        arr /= float(1 << (seg.sample_width * 8 - 1))
        logger.debug("Audio decoded via pydub: %d samples", len(arr))
        return arr
    except Exception as _e:
        logger.debug("pydub failed (%s), writing to safe temp path", _e)

    # Method 3: write to safe no-space path, pass to Whisper as last resort
    safe_sfx = suffix if suffix.startswith(".") else f".{suffix}"
    tmp_path = str(_SAFE_TMP_DIR / f"stt_{uuid.uuid4().hex}{safe_sfx}")
    with open(tmp_path, "wb") as f:
        f.write(audio_bytes)
    logger.warning(
        "In-memory decoders failed -- passing file path to Whisper: %s", tmp_path
    )
    return tmp_path  # Whisper accepts a file path too


# -- STTEngine -----------------------------------------------------------------

class STTEngine:
    """
    Speech-to-text using OpenAI Whisper.

    Language auto-detection:
      Whisper detects language from the first ~30s of audio when language=None.
      Handles English, Hindi, and Hinglish code-switching natively.

    To force a language: COPILOT_STT_LANG=hi (Hindi) or =en (English).
    """

    def transcribe_bytes(
        self,
        audio_bytes: bytes,
        lang: str | None = None,
        suffix: str = ".wav",
    ) -> str:
        """
        Transcribe raw audio bytes to text.
        No ffmpeg on PATH required -- uses soundfile for WAV decoding.
        """
        if not audio_bytes:
            raise ValueError("audio_bytes is empty -- nothing to transcribe")

        audio_input = _audio_bytes_to_numpy(audio_bytes, suffix)
        return self._transcribe(audio_input, lang)

    def transcribe_file(self, path: str | Path, lang: str | None = None) -> str:
        """Transcribe an audio file on disk."""
        path = str(path)
        with open(path, "rb") as f:
            audio_bytes = f.read()
        suffix = Path(path).suffix or ".wav"
        audio_input = _audio_bytes_to_numpy(audio_bytes, suffix)
        return self._transcribe(audio_input, lang)

    def _transcribe(self, audio_input, lang: str | None) -> str:
        """
        Core transcription. Accepts numpy array or file path.

        Language resolution order:
          1. lang argument (per-call override)
          2. STT_LANG env var
          3. "auto" / "" -> None (Whisper auto-detects)
        """
        model = _load_model()

        resolved_lang = lang or STT_LANG
        if resolved_lang.lower() in ("auto", "", "none"):
            resolved_lang = None   # None = Whisper auto-detect (handles Hinglish)

        logger.debug(
            "Transcribing -- model: %s | lang: %s | input_type: %s",
            WHISPER_MODEL,
            resolved_lang or "auto-detect",
            type(audio_input).__name__,
        )

        try:
            result = model.transcribe(
                audio_input,
                language=resolved_lang,
                fp16=False,         # CPU compatibility
                task="transcribe",  # preserves original language
            )
        except Exception as exc:
            logger.error("Whisper transcription failed: %s", exc)
            raise RuntimeError(f"Transcription failed: {exc}") from exc

        text     = (result.get("text") or "").strip()
        detected = result.get("language", resolved_lang or "auto")
        logger.info("Transcription [lang=%s]: %s", detected, text[:120])
        return text


# -- Singleton -----------------------------------------------------------------
stt_engine = STTEngine()