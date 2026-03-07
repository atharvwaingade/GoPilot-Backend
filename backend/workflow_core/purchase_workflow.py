import logging

from workflow_core.base_workflow import BaseWorkflow, WorkflowResult

logger = logging.getLogger(__name__)


class PurchaseWorkflow(BaseWorkflow):
    """
    Deterministic workflow for purchase order entry.
    Covers vendor selection, item lines, quantities, pricing,
    and approval gating for high-value orders.
    """

    required_fields = [
        "vendor_id",
        "purchase_date",
        "delivery_address",
        "item_code",
        "quantity",
        "unit_price",
    ]

    execution_order = [
        "vendor_id",
        "purchase_date",
        "delivery_address",
        "item_code",
        "quantity",
        "unit_price",
        "discount_pct",
        "tax_code",
        "line_total",
        "order_total",
        "notes",
        "approver_id",
    ]

    calculated_fields = [
        "line_total",
        "order_total",
    ]

    financial_fields = [
        "unit_price",
        "discount_pct",
        "line_total",
        "order_total",
    ]

    confirmation_required = True

    # Orders above this threshold require an approver
    APPROVAL_THRESHOLD = 10_000.0

    def validate(self, screen_context: dict) -> list[str]:
        fields = self._extract_fields(screen_context)
        errors: list[str] = []

        for field_id in self.required_fields:
            if self._is_empty(self._get_value(fields, field_id)):
                errors.append(f"Required field missing: {field_id}")

        qty = self._get_value(fields, "quantity")
        if qty is not None:
            try:
                if float(qty) <= 0:
                    errors.append("quantity must be greater than zero")
            except (TypeError, ValueError):
                errors.append("quantity must be a valid number")

        unit_price = self._get_value(fields, "unit_price")
        if unit_price is not None:
            try:
                if float(unit_price) < 0:
                    errors.append("unit_price cannot be negative")
            except (TypeError, ValueError):
                errors.append("unit_price must be a valid number")

        discount = self._get_value(fields, "discount_pct")
        if discount is not None:
            try:
                d = float(discount)
                if not (0.0 <= d <= 100.0):
                    errors.append("discount_pct must be between 0 and 100")
            except (TypeError, ValueError):
                errors.append("discount_pct must be a valid number")

        order_total = self._get_value(fields, "order_total")
        if order_total is not None:
            try:
                if float(order_total) >= self.APPROVAL_THRESHOLD:
                    approver = self._get_value(fields, "approver_id")
                    if self._is_empty(approver):
                        errors.append(
                            f"approver_id required for orders >= {self.APPROVAL_THRESHOLD}"
                        )
            except (TypeError, ValueError):
                pass

        return errors

    def next_step(self, screen_context: dict) -> WorkflowResult:
        fields = self._extract_fields(screen_context)
        errors = self.validate(screen_context)

        next_field = self._first_empty_required(fields)

        # After all required fields are filled, check approval gate
        if next_field is None:
            order_total = self._get_value(fields, "order_total")
            try:
                if float(order_total or 0) >= self.APPROVAL_THRESHOLD:
                    approver = self._get_value(fields, "approver_id")
                    if self._is_empty(approver):
                        next_field = "approver_id"
            except (TypeError, ValueError):
                pass

        self._log_step("PurchaseWorkflow", next_field)

        return WorkflowResult(
            next_field=next_field,
            is_complete=(next_field is None and not errors),
            errors=errors,
        )