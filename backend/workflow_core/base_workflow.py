import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class WorkflowResult:
    next_field: str | None
    is_complete: bool = False
    errors: list[str] = field(default_factory=list)


class BaseWorkflow(ABC):
    """
    Abstract base for all deterministic workflow engines.
    Subclasses declare their field metadata and implement
    validate() and next_step() without any AI logic.
    """

    # Each subclass declares these as class-level attributes
    required_fields: list[str] = []
    execution_order: list[str] = []
    calculated_fields: list[str] = []
    financial_fields: list[str] = []
    confirmation_required: bool = False

    @abstractmethod
    def validate(self, screen_context: dict) -> list[str]:
        """
        Validate the screen context against workflow rules.
        Returns a list of error strings. Empty list means valid.
        """

    @abstractmethod
    def next_step(self, screen_context: dict) -> WorkflowResult:
        """
        Determine the next field that needs user input.
        Returns a WorkflowResult with next_field=None when workflow is complete.
        """

    def _extract_fields(self, screen_context: dict) -> dict[str, dict]:
        """Flatten all fields from all sections into a field_id -> field dict."""
        fields: dict[str, dict] = {}
        for section in screen_context.get("sections", []):
            for f in section.get("fields", []):
                fid = f.get("field_id")
                if fid:
                    fields[fid] = f
        return fields

    def _get_value(self, fields: dict[str, dict], field_id: str):
        f = fields.get(field_id, {})
        return f.get("value")

    def _is_empty(self, value) -> bool:
        return value is None or str(value).strip() == ""

    def _first_empty_required(self, fields: dict[str, dict]) -> str | None:
        """Return the first required field in execution_order that has no value."""
        for field_id in self.execution_order:
            if field_id in self.required_fields:
                if self._is_empty(self._get_value(fields, field_id)):
                    return field_id
        return None

    def _log_step(self, workflow_name: str, next_field: str | None) -> None:
        if next_field:
            logger.debug("%s — next required field: %s", workflow_name, next_field)
        else:
            logger.debug("%s — all required fields satisfied", workflow_name)