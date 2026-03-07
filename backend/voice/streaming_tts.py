"""
streaming_tts.py — Streaming TTS engine (Stage 3.6)

Splits a response into sentences and synthesises + streams each one
independently. The first sentence starts playing within ~300ms instead
of waiting for the full response to be synthesised (~2-3s).

Architecture:
  speak_streaming(text, on_chunk)
    → splits into sentences
    → for each sentence:
        synthesise WAV  (Kokoro ~200ms per sentence)
        call on_chunk(wav_path)   ← caller plays it immediately

WebSocket integration (ws_server.py):
  The on_chunk callback sends tts_chunk events to the popup, which
  chains audio playback so chunks play back-to-back without gaps.

Usage:
  from voice.streaming_tts import speak_streaming

  def send_to_browser(wav_path):
      ws.send_json({"type": "tts_chunk", "audio_url": f"/voice/audio/{Path(wav_path).name}"})

  speak_streaming("This is sentence one. And sentence two.", send_to_browser)

Fallback:
  If split produces only one sentence, behaves identically to TTSEngine.synthesise().
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# ── Sentence splitter ─────────────────────────────────────────────────────
# Splits on sentence-ending punctuation, keeping punctuation with the sentence.
# Handles common abbreviations (Mr., Dr., etc.) to avoid false splits.
_ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "vs", "etc", "inc",
    "ltd", "pvt", "co", "corp", "no", "vol", "approx", "est", "dept",
}
_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\u0900-\u0D7F\u0A00-\u0A7F])")


def split_sentences(text: str) -> list[str]:
    """
    Split text into speakable sentence chunks (~1-2 sentences each).

    Strategy:
      1. Split on sentence boundaries
      2. Skip abbreviations (Mr., Dr., etc.)
      3. Merge very short chunks (<20 chars) with the next one
      4. Cap at ~150 chars per chunk for best TTS latency
    """
    text = text.strip()
    if not text:
        return []

    # Simple split on ". ", "! ", "? " followed by capital
    raw = _SPLIT_RE.split(text)

    chunks: list[str] = []
    carry = ""

    for part in raw:
        part = part.strip()
        if not part:
            continue

        # Check if this ends with an abbreviation (false split)
        words = part.rstrip(".").split()
        last_word = words[-1].lower().rstrip(".") if words else ""
        if last_word in _ABBREV and not part.endswith(("!", "?")):
            carry = (carry + " " + part).strip()
            continue

        combined = (carry + " " + part).strip() if carry else part
        carry = ""

        # Merge very short fragments with the next
        if len(combined) < 25 and chunks:
            chunks[-1] = chunks[-1] + " " + combined
        else:
            chunks.append(combined)

    if carry:
        if chunks:
            chunks[-1] = chunks[-1] + " " + carry
        else:
            chunks.append(carry)

    return [c for c in chunks if c.strip()]


# ── StreamingTTS ──────────────────────────────────────────────────────────

class StreamingTTS:
    """
    Sentence-by-sentence TTS streamer.

    Synthesises each sentence in a thread pool and calls on_chunk
    as soon as each WAV is ready, so playback starts immediately.
    """

    def __init__(self, tts_engine) -> None:
        self._tts     = tts_engine
        self._pool    = ThreadPoolExecutor(max_workers=2, thread_name_prefix="streaming_tts")

    def speak_streaming(
        self,
        text:     str,
        on_chunk: Callable[[str], None],
        *,
        min_chunk_chars: int = 15,
    ) -> list[str]:
        """
        Synthesise text sentence-by-sentence, calling on_chunk for each WAV.

        Args:
            text:            Full response text.
            on_chunk:        Callback called with WAV path as each chunk is ready.
                             Called in the order sentences appear in text.
            min_chunk_chars: Minimum chars to bother synthesising a chunk.

        Returns:
            List of WAV file paths in sentence order.
        """
        sentences = split_sentences(text)
        if not sentences:
            return []

        # Single sentence — skip streaming overhead
        if len(sentences) == 1:
            wav = self._tts.synthesise(sentences[0])
            on_chunk(wav)
            return [wav]

        logger.info("Streaming TTS: %d sentences from %d chars",
                    len(sentences), len(text))

        wav_paths: list[str | None] = [None] * len(sentences)
        t0 = time.monotonic()

        # Submit all synthesis jobs to thread pool
        futures = []
        for i, sentence in enumerate(sentences):
            if len(sentence.strip()) < min_chunk_chars:
                wav_paths[i] = ""  # skip tiny fragments
                futures.append(None)
                continue
            fut = self._pool.submit(self._synthesise_one, i, sentence)
            futures.append(fut)

        # Collect in order and call on_chunk as each finishes
        for i, fut in enumerate(futures):
            if fut is None:
                continue
            try:
                idx, wav_path = fut.result(timeout=10)
                wav_paths[idx] = wav_path
                on_chunk(wav_path)
                logger.debug("TTS chunk %d/%d ready in %.0fms",
                             i + 1, len(sentences),
                             (time.monotonic() - t0) * 1000)
            except Exception as exc:
                logger.warning("TTS chunk %d failed: %s", i, exc)

        return [p for p in wav_paths if p]

    def _synthesise_one(self, idx: int, sentence: str) -> tuple[int, str]:
        """Synthesise a single sentence. Returns (idx, wav_path)."""
        wav = self._tts.synthesise(sentence.strip())
        return idx, wav

    def speak_simple(self, text: str) -> str:
        """Non-streaming synthesis. Identical to tts_engine.synthesise()."""
        return self._tts.synthesise(text)


# ── Chunked text helper ───────────────────────────────────────────────────

def chunk_for_streaming(text: str, max_chars: int = 120) -> list[str]:
    """
    Split text into chunks of ≤max_chars, breaking at sentence boundaries.
    Used by the WebSocket server to send partial text to the frontend
    before full TTS synthesis is complete.
    """
    sentences = split_sentences(text)
    chunks:  list[str] = []
    current: str       = ""

    for s in sentences:
        if len(current) + len(s) + 1 <= max_chars:
            current = (current + " " + s).strip()
        else:
            if current:
                chunks.append(current)
            current = s

    if current:
        chunks.append(current)

    return chunks


# ── Module singleton (lazy — avoids loading TTS at import time) ───────────
_streaming_tts: StreamingTTS | None = None


def get_streaming_tts() -> StreamingTTS:
    global _streaming_tts
    if _streaming_tts is None:
        from voice.tts_engine import tts_engine
        _streaming_tts = StreamingTTS(tts_engine)
    return _streaming_tts