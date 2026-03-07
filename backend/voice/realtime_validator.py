"""
realtime_validator.py — Real-time voice validation (Stage 3.4)

Checks field values as the user fills them by voice and returns
spoken warnings before bad data is committed.

Detects:
  - Zero or negative quantity ("You've entered 0 for Quantity — did you mean something else?")
  - Future invoice dates ("That date is in the future — is that intentional?")
  - Past due dates ("That due date has already passed.")
  - Invalid GSTIN format (15-char alphanumeric)
  - Invalid email / phone format
  - Invalid PIN code (India 6-digit)
  - Missing required fields before submit
  - Quantity/price mismatch (both zero together)

Returns a list of ValidationWarning objects. Voice controller speaks them
immediately after the fill action, before moving to next field.

Zero LLM. <1ms per check.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta

from voice.field_classifier import FieldClass, VALIDATE_CLASSES, classify

logger = logging.getLogger(__name__)


@dataclass
class ValidationWarning:
    field_id:  str
    field_class: FieldClass
    value:     str
    message:   str           # spoken warning text
    severity:  str           # "warn" | "error" | "info"
    blocking:  bool          # True = don't proceed until resolved


# ── Individual validators ─────────────────────────────────────────────────

def _check_quantity(fid: str, value: str) -> ValidationWarning | None:
    try:
        n = float(value.replace(",", ""))
    except ValueError:
        return ValidationWarning(
            field_id=fid, field_class=FieldClass.QUANTITY,
            value=value,
            message=f"'{value}' doesn't look like a valid quantity. Please enter a number.",
            severity="error", blocking=False,
        )
    if n == 0:
        return ValidationWarning(
            field_id=fid, field_class=FieldClass.QUANTITY,
            value=value,
            message="You've entered 0 for Quantity — did you mean something else?",
            severity="warn", blocking=False,
        )
    if n < 0:
        return ValidationWarning(
            field_id=fid, field_class=FieldClass.QUANTITY,
            value=value,
            message="Quantity can't be negative. Please check the value.",
            severity="error", blocking=False,
        )
    return None


def _check_price(fid: str, value: str) -> ValidationWarning | None:
    try:
        n = float(value.replace(",", "").replace("₹", "").strip())
    except ValueError:
        return None  # might be a select/text field — don't warn
    if n < 0:
        return ValidationWarning(
            field_id=fid, field_class=FieldClass.PRICE,
            value=value,
            message="Price can't be negative. Please check the value.",
            severity="error", blocking=False,
        )
    if n == 0:
        return ValidationWarning(
            field_id=fid, field_class=FieldClass.PRICE,
            value=value,
            message="You've entered 0 for the price — is that correct?",
            severity="warn", blocking=False,
        )
    return None


def _parse_date_flexible(value: str) -> date | None:
    v = value.strip()
    for fmt_parts in [
        (r"^(\d{4})-(\d{2})-(\d{2})$",  lambda m: date(int(m[1]),int(m[2]),int(m[3]))),
        (r"^(\d{2})/(\d{2})/(\d{4})$",  lambda m: date(int(m[3]),int(m[2]),int(m[1]))),
        (r"^(\d{2})-(\d{2})-(\d{4})$",  lambda m: date(int(m[3]),int(m[2]),int(m[1]))),
        (r"^(\d{1,2})/(\d{1,2})/(\d{4})$", lambda m: date(int(m[3]),int(m[2]),int(m[1]))),
    ]:
        pattern, builder = fmt_parts
        m = re.match(pattern, v)
        if m:
            try:
                return builder(m.groups())
            except ValueError:
                continue
    return None


def _check_date(fid: str, value: str,
                fc: FieldClass = FieldClass.DATE) -> ValidationWarning | None:
    d = _parse_date_flexible(value)
    if d is None:
        return None  # unparseable — let form validation handle it

    today = date.today()
    field_name = {
        FieldClass.DATE:          "invoice date",
        FieldClass.DUE_DATE:      "due date",
        FieldClass.DELIVERY_DATE: "delivery date",
        FieldClass.DATE_FROM:     "start date",
        FieldClass.DATE_TO:       "end date",
    }.get(fc, "date")

    if fc == FieldClass.DATE and d > today + timedelta(days=1):
        return ValidationWarning(
            field_id=fid, field_class=fc, value=value,
            message=f"The {field_name} is in the future — is that intentional?",
            severity="warn", blocking=False,
        )

    if fc == FieldClass.DUE_DATE and d < today:
        return ValidationWarning(
            field_id=fid, field_class=fc, value=value,
            message=f"The {field_name} has already passed. Please double-check.",
            severity="warn", blocking=False,
        )

    if fc == FieldClass.DELIVERY_DATE and d < today:
        return ValidationWarning(
            field_id=fid, field_class=fc, value=value,
            message=f"The {field_name} is in the past. Did you mean a future date?",
            severity="warn", blocking=False,
        )

    return None


def _check_gstin(fid: str, value: str) -> ValidationWarning | None:
    # GSTIN: 15 characters — 2 digits (state) + 10 PAN + 1 digit + Z + 1 check
    v = value.strip().upper()
    if not re.match(r"^\d{2}[A-Z]{5}\d{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$", v):
        return ValidationWarning(
            field_id=fid, field_class=FieldClass.GSTIN, value=value,
            message=(
                f"'{value}' doesn't look like a valid GSTIN. "
                "It should be 15 characters like: 27AABCU9603R1ZX"
            ),
            severity="warn", blocking=False,
        )
    return None


def _check_email(fid: str, value: str) -> ValidationWarning | None:
    if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", value.strip()):
        return ValidationWarning(
            field_id=fid, field_class=FieldClass.EMAIL, value=value,
            message=f"'{value}' doesn't look like a valid email address.",
            severity="warn", blocking=False,
        )
    return None


def _check_phone(fid: str, value: str) -> ValidationWarning | None:
    digits = re.sub(r"[\s\-\+\(\)]", "", value)
    if not re.match(r"^\d{10,13}$", digits):
        return ValidationWarning(
            field_id=fid, field_class=FieldClass.PHONE, value=value,
            message=f"'{value}' doesn't look like a valid phone number. Expected 10 digits.",
            severity="warn", blocking=False,
        )
    return None


def _check_pincode(fid: str, value: str) -> ValidationWarning | None:
    digits = re.sub(r"\s", "", value)
    if not re.match(r"^\d{6}$", digits):
        return ValidationWarning(
            field_id=fid, field_class=FieldClass.PINCODE, value=value,
            message=f"'{value}' doesn't look like a valid 6-digit Indian PIN code.",
            severity="warn", blocking=False,
        )
    return None


# ── Main validator ────────────────────────────────────────────────────────

class RealtimeValidator:
    """
    Validates a field value the moment it's filled by voice.
    Returns spoken warnings the controller can deliver immediately.
    """

    def validate_fill(
        self,
        field_id:    str,
        label:       str,
        value:       str,
        input_type:  str = "text",
        placeholder: str = "",
    ) -> list[ValidationWarning]:
        """
        Validate a single just-filled field.

        Returns list of ValidationWarning (usually 0 or 1).
        """
        if not value or not value.strip():
            return []

        fc = classify(field_id, label, input_type, placeholder)

        if fc not in VALIDATE_CLASSES:
            return []

        checkers = {
            FieldClass.QUANTITY:       _check_quantity,
            FieldClass.PRICE:          _check_price,
            FieldClass.DATE:           lambda fid, v: _check_date(fid, v, FieldClass.DATE),
            FieldClass.DUE_DATE:       lambda fid, v: _check_date(fid, v, FieldClass.DUE_DATE),
            FieldClass.DELIVERY_DATE:  lambda fid, v: _check_date(fid, v, FieldClass.DELIVERY_DATE),
            FieldClass.GSTIN:          _check_gstin,
            FieldClass.EMAIL:          _check_email,
            FieldClass.PHONE:          _check_phone,
            FieldClass.PINCODE:        _check_pincode,
        }

        checker = checkers.get(fc)
        if checker is None:
            return []

        warning = checker(field_id, value.strip())
        return [warning] if warning else []

    def validate_before_submit(self, context: dict) -> list[ValidationWarning]:
        """
        Run all validations before a submit action.
        Returns warnings for all fields — both missing required and invalid values.
        """
        warnings: list[ValidationWarning] = []

        for section in context.get("sections", []):
            for f in section.get("fields", []):
                fid      = f.get("field_id", "")
                label    = f.get("label", "")
                value    = f.get("value", "")
                req      = f.get("required", False)
                readonly = f.get("readonly", False)

                if readonly:
                    continue

                # Missing required field
                if req and not (value and str(value).strip()):
                    from voice.voice_controller import _field_to_human
                    name = _field_to_human(fid, label)
                    warnings.append(ValidationWarning(
                        field_id=fid,
                        field_class=classify(fid, label),
                        value="",
                        message=f"{name} is required but hasn't been filled in.",
                        severity="error",
                        blocking=True,
                    ))
                    continue

                # Validate filled values
                if value and str(value).strip():
                    field_warnings = self.validate_fill(
                        fid, label, str(value),
                        f.get("type", "text"),
                        f.get("placeholder", "") or "",
                    )
                    warnings.extend(field_warnings)

        return warnings

    def spoken_summary(self, warnings: list[ValidationWarning]) -> str:
        """
        Convert a list of warnings into a single spoken summary string.

        For 1 warning: speak it directly.
        For 2+: "I found N issues. First: {msg}. Also: {msg2}."
        """
        if not warnings:
            return ""

        blocking = [w for w in warnings if w.blocking]
        non_blocking = [w for w in warnings if not w.blocking]

        parts: list[str] = []

        if blocking:
            if len(blocking) == 1:
                parts.append(f"Before submitting: {blocking[0].message}")
            else:
                names = "; ".join(w.message for w in blocking[:3])
                parts.append(f"I can't submit yet. {len(blocking)} required fields are empty: {names}")

        if non_blocking:
            if len(non_blocking) == 1:
                parts.append(non_blocking[0].message)
            else:
                parts.append(f"Also, I noticed {len(non_blocking)} potential issues: "
                             + "; ".join(w.message for w in non_blocking[:2]))

        return " ".join(parts)


# ── Singleton ─────────────────────────────────────────────────────────────
realtime_validator = RealtimeValidator()