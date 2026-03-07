from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ActionType(str, Enum):
    TOOL_CALL = "tool_call"
    EXPLAIN = "explain"
    CONFIRMATION = "confirmation"
    ERROR = "error"


class ToolCall(BaseModel):
    """
    Instructs the platform to set a field value or trigger an action.
    The LLM must never set calculated or readonly fields.
    """

    action: Literal[ActionType.TOOL_CALL] = ActionType.TOOL_CALL
    field_id: str = Field(..., min_length=1)
    value: Any
    # reason is optional — LLM often omits it or sends empty string.
    # Accept missing/empty and normalise rather than failing validation.
    reason: str = Field(default="")

    @field_validator("field_id")
    @classmethod
    def field_id_no_spaces(cls, v: str) -> str:
        if " " in v:
            raise ValueError("field_id must not contain spaces")
        return v

    @field_validator("reason", mode="before")
    @classmethod
    def reason_default(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v).strip()

    model_config = {"extra": "forbid"}


class ExplainAction(BaseModel):
    """
    Returns a plain-text explanation to surface to the user.
    Used when the LLM cannot determine a concrete next action.
    """

    action: Literal[ActionType.EXPLAIN] = ActionType.EXPLAIN
    message: str = Field(..., min_length=1, max_length=1024)
    related_fields: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class ConfirmationAction(BaseModel):
    """
    Requests explicit user confirmation before proceeding.
    Triggered when confirmation_required=True on the active workflow.
    """

    action: Literal[ActionType.CONFIRMATION] = ActionType.CONFIRMATION
    message: str = Field(..., min_length=1, max_length=1024)
    fields_to_confirm: list[str] = Field(default_factory=list)
    workflow_name: str = Field(..., min_length=1)

    model_config = {"extra": "forbid"}


class ErrorAction(BaseModel):
    """
    Returned when LLM output cannot be parsed or validated after all retries.
    Always safe to surface to the caller.
    """

    action: Literal[ActionType.ERROR] = ActionType.ERROR
    reason: str = Field(..., min_length=1)
    raw_output: str | None = None
    retry_count: int = Field(default=0, ge=0)

    model_config = {"extra": "forbid"}


# Union type used for discriminated parsing
LLMAction = ToolCall | ExplainAction | ConfirmationAction | ErrorAction