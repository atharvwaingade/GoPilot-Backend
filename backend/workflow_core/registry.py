import logging

logger = logging.getLogger(__name__)


class WorkflowRegistry:
    def __init__(self) -> None:
        self._workflows: dict = {}

    def register(self, name: str, workflow) -> None:
        if name in self._workflows:
            raise ValueError(f"Workflow '{name}' is already registered")
        self._workflows[name] = workflow
        logger.debug("Workflow registered: %s", name)

    def get(self, name: str):
        if name not in self._workflows:
            raise KeyError(f"No workflow registered under '{name}'")
        return self._workflows[name]

    def get_best(self, name: str, screen_context: dict):
        """
        Return the best workflow for the given name and live screen context.

        If the requested workflow's execution_order doesn't overlap with
        the actual page field IDs, fall back to FreeWorkflow so the LLM
        can fill any field the user asks about instead of getting stuck.
        """
        workflow = self.get(name)

        # FreeWorkflow always works — skip overlap check
        from workflow_core.free_workflow import FreeWorkflow
        if isinstance(workflow, FreeWorkflow):
            return workflow

        # Check how many of the workflow's fields actually exist on this page
        page_field_ids: set[str] = set()
        for section in screen_context.get("sections", []):
            for f in section.get("fields", []):
                fid = f.get("field_id")
                if fid:
                    page_field_ids.add(fid)

        if not page_field_ids:
            return workflow  # No context yet — use as-is

        workflow_fields = set(workflow.execution_order or [])
        overlap = workflow_fields & page_field_ids
        overlap_pct = len(overlap) / max(len(workflow_fields), 1)

        if overlap_pct < 0.2:
            # Less than 20% overlap — page doesn't match the workflow's field IDs
            # Fall back to FreeWorkflow which reads fields directly from the page
            logger.info(
                "Workflow '%s' has only %.0f%% field overlap with page — using FreeWorkflow",
                name, overlap_pct * 100,
            )
            return self._workflows.get("free", FreeWorkflow())

        return workflow

    def list_workflows(self) -> list[str]:
        return list(self._workflows.keys())


workflow_registry = WorkflowRegistry()

from workflow_core.purchase_workflow  import PurchaseWorkflow
from workflow_core.supplier_workflow  import SupplierWorkflow
from workflow_core.sell_workflow      import SellWorkflow
from workflow_core.free_workflow      import FreeWorkflow

workflow_registry.register("purchase", PurchaseWorkflow())
workflow_registry.register("supplier", SupplierWorkflow())
workflow_registry.register("sell",     SellWorkflow())
workflow_registry.register("free",     FreeWorkflow())