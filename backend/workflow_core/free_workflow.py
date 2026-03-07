import logging

from workflow_core.base_workflow import BaseWorkflow, WorkflowResult

logger = logging.getLogger(__name__)


class FreeWorkflow(BaseWorkflow):
    """
    Universal workflow that works on ANY page with ANY fields.

    Instead of hardcoded field IDs, it reads the actual fields
    from the live screen context and treats all non-readonly,
    non-calculated empty fields as fillable targets.

    This is the fallback when the page fields don't match a
    predefined workflow's execution_order.
    """

    required_fields:   list[str] = []
    execution_order:   list[str] = []
    calculated_fields: list[str] = []
    financial_fields:  list[str] = []
    confirmation_required: bool = False

    def validate(self, screen_context: dict) -> list[str]:
        return []   # No validation rules — free mode

    def next_step(self, screen_context: dict) -> WorkflowResult:
        """
        Return the first visible, non-readonly, non-calculated
        empty field from the live screen context.
        """
        fields = self._extract_fields(screen_context)

        for section in screen_context.get("sections", []):
            for f in section.get("fields", []):
                fid = f.get("field_id")
                if not fid:
                    continue
                if f.get("readonly") or f.get("calculated"):
                    continue
                val = f.get("value")
                if val is None or str(val).strip() == "":
                    logger.debug("FreeWorkflow — next field: %s", fid)
                    return WorkflowResult(next_field=fid, is_complete=False)

        logger.debug("FreeWorkflow — all fields filled or no fillable fields found")
        return WorkflowResult(next_field=None, is_complete=True)

    def get_live_metadata(self, screen_context: dict) -> dict:
        """
        Build required_fields, calculated_fields and execution_order
        dynamically from the live screen context.
        Called by the routing logic before passing to the LLM.
        """
        required:    list[str] = []
        calculated:  list[str] = []
        execution:   list[str] = []

        for section in screen_context.get("sections", []):
            for f in section.get("fields", []):
                fid = f.get("field_id")
                if not fid:
                    continue
                execution.append(fid)
                if f.get("required"):
                    required.append(fid)
                if f.get("calculated") or f.get("readonly"):
                    calculated.append(fid)

        return {
            "required_fields":   required,
            "calculated_fields": calculated,
            "execution_order":   execution,
        }