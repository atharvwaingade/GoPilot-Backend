import hashlib
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LOGS_DIR = Path(__file__).parent / "sessions"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ── Cross-platform file locking ────────────────────────────────────────────

if sys.platform == "win32":
    import msvcrt

    def _lock(fh):
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(fh):
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock(fh):
        fcntl.flock(fh, fcntl.LOCK_EX)

    def _unlock(fh):
        fcntl.flock(fh, fcntl.LOCK_UN)


# ── Audit entry builder ────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_context(screen_context: Any) -> str:
    try:
        serialised = json.dumps(screen_context, sort_keys=True, default=str)
    except (TypeError, ValueError):
        serialised = str(screen_context)
    return hashlib.sha256(serialised.encode()).hexdigest()[:16]


def build_entry(
    session_id: str,
    user_input: str,
    screen_context: Any,
    llm_raw_output: str,
    validated_output: Any,
    tool_executed: str | None,
    result: str,
    mode: str,
    workflow: str,
) -> dict:
    return {
        "timestamp":           _now_iso(),
        "session_id":          session_id,
        "user_input":          user_input,
        "screen_context_hash": _hash_context(screen_context),
        "llm_raw_output":      llm_raw_output,
        "validated_output":    validated_output,
        "tool_executed":       tool_executed,
        "result":              result,
        "mode":                mode,
        "workflow":            workflow,
    }


# ── Writer ─────────────────────────────────────────────────────────────────


class AuditLogger:
    """
    Thread-safe, append-only JSONL audit logger.
    One file per session: logs/sessions/<session_id>.jsonl
    Uses platform-appropriate file locking (msvcrt on Windows, fcntl on Unix).
    """

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()

    def _session_path(self, session_id: str) -> Path:
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
        if not safe:
            raise ValueError(f"session_id '{session_id}' produced an empty safe filename")
        return LOGS_DIR / f"{safe}.jsonl"

    def _get_lock(self, session_id: str) -> threading.Lock:
        with self._meta_lock:
            if session_id not in self._locks:
                self._locks[session_id] = threading.Lock()
            return self._locks[session_id]

    def write(self, entry: dict) -> None:
        session_id = entry.get("session_id", "unknown")
        path = self._session_path(session_id)
        line = json.dumps(entry, default=str) + "\n"

        lock = self._get_lock(session_id)
        with lock:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    _lock(fh)
                    try:
                        fh.write(line)
                        fh.flush()
                        os.fsync(fh.fileno())
                    finally:
                        _unlock(fh)
            except OSError as exc:
                logger.error("Audit write failed for session '%s': %s", session_id, exc)

    def log(
        self,
        session_id: str,
        user_input: str,
        screen_context: Any,
        llm_raw_output: str,
        validated_output: Any,
        tool_executed: str | None,
        result: str,
        mode: str,
        workflow: str,
    ) -> None:
        entry = build_entry(
            session_id=session_id,
            user_input=user_input,
            screen_context=screen_context,
            llm_raw_output=llm_raw_output,
            validated_output=validated_output,
            tool_executed=tool_executed,
            result=result,
            mode=mode,
            workflow=workflow,
        )
        self.write(entry)
        logger.debug(
            "Audit entry written — session: %s, workflow: %s, result: %s",
            session_id, workflow, result,
        )

    def replay(self, session_id: str) -> list[dict]:
        path = self._session_path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"No audit log found for session '{session_id}'")

        entries: list[dict] = []
        lock = self._get_lock(session_id)

        with lock:
            with open(path, "r", encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        logger.warning(
                            "Skipping malformed line %d in session '%s': %s",
                            line_no, session_id, exc,
                        )

        logger.debug("Replay loaded %d entries for session '%s'", len(entries), session_id)
        return entries

    def list_sessions(self) -> list[str]:
        return sorted(p.stem for p in LOGS_DIR.glob("*.jsonl") if p.is_file())

    def session_exists(self, session_id: str) -> bool:
        try:
            return self._session_path(session_id).exists()
        except ValueError:
            return False


audit_logger = AuditLogger()