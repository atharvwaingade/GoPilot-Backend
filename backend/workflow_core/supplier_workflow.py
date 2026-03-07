import logging
import re

from workflow_core.base_workflow import BaseWorkflow, WorkflowResult

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SupplierWorkflow(BaseWorkflow):
    """
    Deterministic workflow for supplier (vendor) registration and maintenance.
    Covers identity, contact, banking, and compliance classification.
    """

    required_fields = [
        "supplier_name",
        "tax_id",
        "contact_email",
        "contact_phone",
        "payment_terms",
        "currency_code",
    ]

    execution_order = [
        "supplier_name",
        "supplier_code",
        "tax_id",
        "contact_name",
        "contact_email",
        "contact_phone",
        "address_line1",
        "address_city",
        "address_country",
        "payment_terms",
        "currency_code",
        "bank_account_no",
        "bank_routing_no",
        "credit_limit",
        "supplier_category",
        "is_active",
    ]

    calculated_fields: list[str] = []

    financial_fields = [
        "credit_limit",
    ]

    confirmation_required = True

    VALID_PAYMENT_TERMS = {"NET15", "NET30", "NET45", "NET60", "IMMEDIATE"}

    def validate(self, screen_context: dict) -> list[str]:
        fields = self._extract_fields(screen_context)
        errors: list[str] = []

        for field_id in self.required_fields:
            if self._is_empty(self._get_value(fields, field_id)):
                errors.append(f"Required field missing: {field_id}")

        email = self._get_value(fields, "contact_email")
        if email and not _EMAIL_RE.match(str(email)):
            errors.append("contact_email format is invalid")

        payment_terms = self._get_value(fields, "payment_terms")
        if payment_terms and str(payment_terms).upper() not in self.VALID_PAYMENT_TERMS:
            errors.append(
                f"payment_terms must be one of: {', '.join(sorted(self.VALID_PAYMENT_TERMS))}"
            )

        currency = self._get_value(fields, "currency_code")
        if currency and len(str(currency)) != 3:
            errors.append("currency_code must be a 3-letter ISO 4217 code")

        credit_limit = self._get_value(fields, "credit_limit")
        if credit_limit is not None:
            try:
                if float(credit_limit) < 0:
                    errors.append("credit_limit cannot be negative")
            except (TypeError, ValueError):
                errors.append("credit_limit must be a valid number")

        bank_account = self._get_value(fields, "bank_account_no")
        bank_routing = self._get_value(fields, "bank_routing_no")
        if not self._is_empty(bank_account) and self._is_empty(bank_routing):
            errors.append("bank_routing_no is required when bank_account_no is provided")
        if not self._is_empty(bank_routing) and self._is_empty(bank_account):
            errors.append("bank_account_no is required when bank_routing_no is provided")

        return errors

    def next_step(self, screen_context: dict) -> WorkflowResult:
        fields = self._extract_fields(screen_context)
        errors = self.validate(screen_context)

        next_field = self._first_empty_required(fields)

        self._log_step("SupplierWorkflow", next_field)

        return WorkflowResult(
            next_field=next_field,
            is_complete=(next_field is None and not errors),
            errors=errors,
        )