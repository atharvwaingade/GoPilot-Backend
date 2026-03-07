from pydantic import BaseModel
from typing import Any


class AuditEntry(BaseModel):
    timestamp: str
    session_id: str
    user_input: str
    screen_context_hash: str
    llm_raw_output: str
    validated_output: Any
    tool_executed: str | None
    result: str
    mode: str
    workflow: str

    model_config = {"extra": "ignore"}


class ReplayResponse(BaseModel):
    session_id: str
    total_entries: int
    entries: list[AuditEntry]