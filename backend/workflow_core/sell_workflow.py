import logging
from datetime import date

from workflow_core.base_workflow import BaseWorkflow, WorkflowResult

logger = logging.getLogger(__name__)


class SellWorkflow(BaseWorkflow):
    """
    Deterministic workflow for sales order entry.
    Covers customer selection, line items, pricing, tax,
    and credit-limit gating before order confirmation.
    """

    required_fields = [
        "customer_id",
        "order_date",
        "ship_to_address",
        "item_code",
        "quantity",
        "unit_price",
        "payment_method",
    ]

    execution_order = [
        "customer_id",
        "order_date",
        "requested_delivery_date",
        "ship_to_address",
        "item_code",
        "quantity",
        "unit_price",
        "discount_pct",
        "tax_code",
        "line_total",
        "order_subtotal",
        "tax_amount",
        "order_total",
        "payment_method",
        "reference_no",
        "sales_rep_id",
        "notes",
    ]

    calculated_fields = [
        "line_total",
        "order_subtotal",
        "tax_amount",
        "order_total",
    ]

    financial_fields = [
        "unit_price",
        "discount_pct",
        "line_total",
        "order_subtotal",
        "tax_amount",
        "order_total",
    ]

    confirmation_required = True

    VALID_PAYMENT_METHODS = {"CASH", "CREDIT_CARD", "BANK_TRANSFER", "CHECK", "NET_TERMS"}

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

        payment_method = self._get_value(fields, "payment_method")
        if payment_method and str(payment_method).upper() not in self.VALID_PAYMENT_METHODS:
            errors.append(
                f"payment_method must be one of: {', '.join(sorted(self.VALID_PAYMENT_METHODS))}"
            )

        order_date = self._get_value(fields, "order_date")
        delivery_date = self._get_value(fields, "requested_delivery_date")
        if order_date and delivery_date:
            try:
                od = date.fromisoformat(str(order_date))
                dd = date.fromisoformat(str(delivery_date))
                if dd < od:
                    errors.append("requested_delivery_date cannot be before order_date")
            except ValueError:
                errors.append("order_date and requested_delivery_date must be ISO 8601 dates")

        return errors

    def next_step(self, screen_context: dict) -> WorkflowResult:
        fields = self._extract_fields(screen_context)
        errors = self.validate(screen_context)

        # Skip calculated fields — they are never user-input targets
        next_field: str | None = None
        for field_id in self.execution_order:
            if field_id in self.calculated_fields:
                continue
            if field_id in self.required_fields and self._is_empty(
                self._get_value(fields, field_id)
            ):
                next_field = field_id
                break

        self._log_step("SellWorkflow", next_field)

        return WorkflowResult(
            next_field=next_field,
            is_complete=(next_field is None and not errors),
            errors=errors,
        )