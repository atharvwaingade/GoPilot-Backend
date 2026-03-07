import logging
import os
import time
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Voice config ───────────────────────────────────────────────────────────
TTS_VOICE_DESC = os.environ.get(
    "COPILOT_TTS_VOICE_DESC",
    (
        "Aditi speaks clearly with a natural Indian English accent at a moderate pace. "
        "The recording is studio quality with no background noise."
    ),
)

# TTS backend selection.
# "kokoro"  — recommended: fast, local, no auth needed, good quality (~300MB)
# "pyttsx3" — zero-install fallback: uses Windows SAPI / espeak, no download needed
# "parler"  — original option, requires HuggingFace gated repo access (401 without login)
TTS_BACKEND = os.environ.get("COPILOT_TTS_BACKEND", "kokoro").lower()

AUDIO_OUT_DIR = Path(__file__).parent.parent / "voice_output"
AUDIO_OUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Kokoro TTS (recommended) ───────────────────────────────────────────────
_kokoro_pipeline = None

def _load_kokoro():
    global _kokoro_pipeline
    if _kokoro_pipeline is not None:
        return _kokoro_pipeline
    try:
        from kokoro import KPipeline
        # 'a' = American English; use 'b' for British, or 'h' for Hindi
        _kokoro_pipeline = KPipeline(lang_code="a")
        logger.info("Kokoro TTS loaded successfully")
        return _kokoro_pipeline
    except ImportError as exc:
        raise RuntimeError(
            "kokoro is not installed. Run:\n"
            "  pip install kokoro soundfile\n"
            "  # on Windows also: pip install pywin32"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to load Kokoro TTS: {exc}") from exc


def _synthesise_kokoro(text: str, out_path: Path) -> None:
    import soundfile as sf
    import numpy as np
    pipeline = _load_kokoro()
    # voice "af_heart" is a clear, neutral English voice
    generator = pipeline(text, voice="af_heart", speed=1.0)
    audio_chunks = []
    for _, _, audio in generator:
        audio_chunks.append(audio)
    if not audio_chunks:
        raise RuntimeError("Kokoro returned no audio")
    audio_array = np.concatenate(audio_chunks)
    sf.write(str(out_path), audio_array, 24000)
    logger.debug("Kokoro wrote %.1fs audio", len(audio_array) / 24000)


# ── pyttsx3 TTS (zero-install fallback) ───────────────────────────────────

def _synthesise_pyttsx3(text: str, out_path: Path) -> None:
    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", 165)
        engine.save_to_file(text, str(out_path))
        engine.runAndWait()
        logger.debug("pyttsx3 wrote TTS to %s", out_path)
    except ImportError as exc:
        raise RuntimeError(
            "pyttsx3 is not installed. Run: pip install pyttsx3"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"pyttsx3 failed: {exc}") from exc


# ── Parler TTS (gated — requires HuggingFace login) ───────────────────────
_tts_model        = None
_prompt_tokenizer = None
_desc_tokenizer   = None


def _load_parler():
    global _tts_model, _prompt_tokenizer, _desc_tokenizer
    if _tts_model is not None:
        return
    try:
        import torch
        from parler_tts import ParlerTTSForConditionalGeneration
        from transformers import AutoTokenizer

        logger.info("Loading Indic Parler-TTS (requires HuggingFace login)...")
        _tts_model = ParlerTTSForConditionalGeneration.from_pretrained(
            "ai4bharat/indic-parler-tts"
        ).to("cpu")
        _tts_model.eval()
        _prompt_tokenizer = AutoTokenizer.from_pretrained("ai4bharat/indic-parler-tts")
        _desc_tokenizer = AutoTokenizer.from_pretrained(
            _tts_model.config.text_encoder._name_or_path
        )
        logger.info("Indic Parler-TTS loaded successfully")
    except Exception as exc:
        raise RuntimeError(f"Failed to load Indic Parler-TTS: {exc}") from exc


def _synthesise_parler(text: str, description: str, out_path: Path) -> None:
    import torch
    import soundfile as sf
    _load_parler()
    desc_inputs   = _desc_tokenizer(description, return_tensors="pt")
    prompt_inputs = _prompt_tokenizer(text, return_tensors="pt")
    with torch.no_grad():
        generation = _tts_model.generate(
            input_ids=desc_inputs.input_ids,
            attention_mask=desc_inputs.attention_mask,
            prompt_input_ids=prompt_inputs.input_ids,
            prompt_attention_mask=prompt_inputs.attention_mask,
        )
    audio_array = generation.cpu().numpy().squeeze()
    sf.write(str(out_path), audio_array, _tts_model.config.sampling_rate)


# ── TTSEngine ──────────────────────────────────────────────────────────────

class TTSEngine:
    """
    Text-to-speech engine with multiple backend options.

    Backend priority (set COPILOT_TTS_BACKEND env var):
      kokoro  — recommended: fast local TTS, no auth, ~300MB download
      pyttsx3 — zero-install: uses Windows SAPI/espeak, no download
      parler  — original: requires HuggingFace gated repo access

    Falls back to silent WAV if synthesis fails, so the API contract
    (always returns a file path) is never broken.
    """

    def __init__(self) -> None:
        self._check_playback_deps()

    def _check_playback_deps(self) -> None:
        try:
            import sounddevice  # noqa
            import soundfile    # noqa
            self._playback_ok = True
        except ImportError:
            self._playback_ok = False

    def synthesise(self, text: str, voice_desc: str | None = None, play: bool = False) -> str:
        if not text or not text.strip():
            raise ValueError("Cannot synthesise empty text")

        text     = text.strip()[:500]
        out_path = AUDIO_OUT_DIR / f"tts_{int(time.time() * 1000)}.wav"

        try:
            if TTS_BACKEND == "kokoro":
                _synthesise_kokoro(text, out_path)
            elif TTS_BACKEND == "pyttsx3":
                _synthesise_pyttsx3(text, out_path)
            elif TTS_BACKEND == "parler":
                _synthesise_parler(text, voice_desc or TTS_VOICE_DESC, out_path)
            else:
                logger.warning("Unknown TTS_BACKEND '%s', falling back to pyttsx3", TTS_BACKEND)
                _synthesise_pyttsx3(text, out_path)
        except RuntimeError as exc:
            logger.error("TTS failed (%s), writing silent fallback: %s", TTS_BACKEND, exc)
            self._write_silent_wav(out_path)

        if play and self._playback_ok:
            self._play_wav(out_path)

        logger.info("TTS output: %s (%d chars)", out_path.name, len(text))
        return str(out_path)

    def _write_silent_wav(self, path: Path, duration_ms: int = 500) -> None:
        sample_rate = 22050
        num_frames  = sample_rate * duration_ms // 1000
        with wave.open(str(path), "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(b"\x00\x00" * num_frames)
        logger.info("Silent fallback WAV written: %s", path)

    def _play_wav(self, path: Path) -> None:
        try:
            import sounddevice as sd
            import soundfile as sf
            data, samplerate = sf.read(str(path))
            sd.play(data, samplerate)
            sd.wait()
        except Exception as exc:
            logger.warning("Playback failed: %s", exc)


# ── Singleton ──────────────────────────────────────────────────────────────
tts_engine = TTSEngine()