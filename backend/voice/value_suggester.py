"""
value_suggester.py — Predictive value suggestions (Stage 3.2)

After a user fills a field, suggests sensible values for related fields
based on:
  1. What the user just filled (trigger → suggestion rules)
  2. Session history (repeat values from past fills)
  3. Static lookup tables (GST rates, HSN codes, payment terms, etc.)

Zero LLM. <1ms per suggestion call.

Examples:
  User sets Category = "Furniture"
    → Suggests HSN code 9403 for furniture
    → Suggests GST rate 18%

  User sets Supply Type = "Intrastate"
    → Suggests CGST 9%, SGST 9%, IGST 0%

  User sets Supplier = "ABC Ltd" (filled before)
    → Suggests last used Payment Mode for ABC Ltd
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from voice.field_classifier import FieldClass, classify

logger = logging.getLogger(__name__)


@dataclass
class Suggestion:
    field_id:    str
    field_class: FieldClass
    value:       str
    reason:      str          # spoken: "Based on Furniture category, HSN 9403 is typical."
    confidence:  float        # 0.0–1.0


# ── GST rate table: category keyword → GST% ───────────────────────────────
# Source: India GST schedule (simplified)
_CATEGORY_GST: dict[str, int] = {
    # 0%
    "grain":       0, "rice":        0, "wheat":    0, "milk":       0,
    "egg":         0, "vegetable":   0, "fruit":    0, "salt":       0,
    "bread":       0, "newspaper":   0, "book":     0, "educational":0,

    # 5%
    "sugar":       5, "tea":         5, "coffee":   5, "coal":       5,
    "medicine":    5, "pharma":      5, "fabric":   5, "footwear":   5,
    "agro":        5, "agriculture": 5, "fish":     5, "spice":      5,

    # 12%
    "butter":     12, "cheese":     12, "ghee":    12, "fruit juice":12,
    "umbrella":   12, "mobile":     12, "phone":   12, "computer":   12,
    "laptop":     12, "tablet":     12, "printer": 12, "camera":     12,
    "watch":      12, "pen":        12, "stationery":12,

    # 18%
    "furniture":  18, "wood":       18, "steel":   18, "iron":       18,
    "plastic":    18, "chemical":   18, "paint":   18, "varnish":    18,
    "soap":       18, "detergent":  18, "shampoo": 18, "cosmetic":   18,
    "electronic": 18, "electrical": 18, "cable":   18, "wire":       18,
    "machine":    18, "equipment":  18, "tool":    18, "hardware":   18,
    "cloth":      18, "textile":    18, "garment": 18,
    "restaurant": 18, "hotel":      18, "service": 18,

    # 28%
    "car":        28, "vehicle":    28, "motor":   28, "cement":     28,
    "tobacco":    28, "cigarette":  28, "pan":     28, "luxury":     28,
    "perfume":    28, "aerated":    28,
}

# ── HSN code table: category keyword → (HSN, description) ─────────────────
_CATEGORY_HSN: dict[str, tuple[str, str]] = {
    "furniture":  ("9403", "Furniture and parts thereof"),
    "chair":      ("9401", "Seats and parts thereof"),
    "table":      ("9403", "Furniture"),
    "wood":       ("4407", "Wood sawn or chipped"),
    "steel":      ("7208", "Flat-rolled products of iron or steel"),
    "iron":       ("7203", "Ferrous products"),
    "plastic":    ("3926", "Other articles of plastics"),
    "fabric":     ("5208", "Woven fabrics of cotton"),
    "cloth":      ("5208", "Woven fabrics of cotton"),
    "garment":    ("6201", "Men's or boys' overcoats"),
    "footwear":   ("6401", "Waterproof footwear"),
    "shoe":       ("6403", "Footwear with outer soles of rubber"),
    "mobile":     ("8517", "Telephone sets"),
    "phone":      ("8517", "Telephone sets"),
    "computer":   ("8471", "Automatic data processing machines"),
    "laptop":     ("8471", "Portable computers"),
    "tablet":     ("8471", "Tablets"),
    "printer":    ("8443", "Printing machinery"),
    "camera":     ("9006", "Photographic cameras"),
    "electronic": ("8542", "Electronic integrated circuits"),
    "electrical": ("8544", "Insulated wire and cable"),
    "medicine":   ("3004", "Medicaments for retail sale"),
    "pharma":     ("3004", "Medicaments"),
    "soap":       ("3401", "Soap and organic surface-active products"),
    "detergent":  ("3402", "Organic surface-active agents"),
    "cosmetic":   ("3304", "Beauty or make-up preparations"),
    "paint":      ("3208", "Paints and varnishes"),
    "cement":     ("2523", "Portland cement"),
    "chemical":   ("2801", "Chemical elements"),
    "machine":    ("8479", "Machines and mechanical appliances"),
    "tool":       ("8205", "Hand tools"),
    "paper":      ("4802", "Uncoated paper and paperboard"),
    "book":       ("4901", "Printed books, brochures"),
    "rice":       ("1006", "Rice"),
    "wheat":      ("1001", "Wheat and meslin"),
    "sugar":      ("1701", "Cane or beet sugar"),
    "tea":        ("0902", "Tea"),
    "coffee":     ("0901", "Coffee"),
    "milk":       ("0401", "Milk and cream"),
    "vegetable":  ("0709", "Other vegetables"),
    "fruit":      ("0809", "Apricots, cherries, peaches"),
}

# ── Payment mode suggestions per supplier type ────────────────────────────
_PAYMENT_MODES = ["Cash", "Bank Transfer", "Cheque", "UPI", "Credit",
                  "NEFT", "RTGS", "DD"]

# ── Standard payment terms ────────────────────────────────────────────────
_PAYMENT_TERMS = ["Immediate", "Net 7", "Net 15", "Net 30", "Net 45",
                  "Net 60", "Due on receipt"]


class ValueSuggester:
    """
    Rule-based predictive value suggestions.

    Called from voice_controller.py after every successful field fill.
    Returns a list of Suggestion objects that voice_controller can speak.
    """

    def __init__(self) -> None:
        # Session memory: field_class → last value seen in this session
        self._session: dict[FieldClass, str] = {}
        # Supplier-specific memory: supplier_name → {field_class: value}
        self._supplier_memory: dict[str, dict[FieldClass, str]] = {}

    def record(self, field_id: str, label: str, value: str,
               context: dict | None = None) -> None:
        """Record a filled field into session memory."""
        fc = classify(field_id, label)
        if not value or not value.strip():
            return
        self._session[fc] = value.strip()

        # Supplier-specific memory
        if fc == FieldClass.SUPPLIER:
            self._supplier_memory.setdefault(value.strip(), {})
        elif context:
            # Find current supplier in context
            for section in context.get("sections", []):
                for f in section.get("fields", []):
                    if classify(f.get("field_id", ""), f.get("label", "")) == FieldClass.SUPPLIER:
                        sup = f.get("value", "")
                        if sup:
                            self._supplier_memory.setdefault(sup, {})[fc] = value.strip()

    def suggest(
        self,
        just_filled_id:    str,
        just_filled_label: str,
        just_filled_value: str,
        context:           dict,
    ) -> list[Suggestion]:
        """
        Generate suggestions triggered by the field that was just filled.

        Returns suggestions for OTHER empty fields on the page.
        """
        trigger_class = classify(just_filled_id, just_filled_label)
        value_lower   = just_filled_value.lower().strip()

        suggestions: list[Suggestion] = []
        empty_fields = _empty_fillable_fields(context)

        # ── Category → HSN code ───────────────────────────────────────────
        if trigger_class == FieldClass.CATEGORY:
            hsn_fields = [f for f in empty_fields
                          if classify(f.get("field_id",""), f.get("label",""))
                          == FieldClass.HSN_SAC]
            if hsn_fields:
                hsn_fid = hsn_fields[0].get("field_id", "")
                hsn_lbl = hsn_fields[0].get("label", "")
                hsn, desc = _lookup_hsn(value_lower)
                if hsn:
                    suggestions.append(Suggestion(
                        field_id=hsn_fid, field_class=FieldClass.HSN_SAC,
                        value=hsn,
                        reason=f"Based on '{just_filled_value}' category, HSN {hsn} ({desc}) is standard.",
                        confidence=0.75,
                    ))

        # ── Category → GST rate ───────────────────────────────────────────
        if trigger_class in (FieldClass.CATEGORY, FieldClass.PRODUCT_NAME):
            gst_rate = _lookup_gst_rate(value_lower)
            if gst_rate is not None:
                # Look for tax rate field
                tax_fields = [f for f in empty_fields
                              if classify(f.get("field_id",""), f.get("label",""))
                              in (FieldClass.TAX_RATE, FieldClass.CGST,
                                  FieldClass.SGST, FieldClass.IGST)]
                for tf in tax_fields[:1]:
                    fc = classify(tf.get("field_id",""), tf.get("label",""))
                    if fc == FieldClass.TAX_RATE:
                        suggestions.append(Suggestion(
                            field_id=tf.get("field_id",""),
                            field_class=FieldClass.TAX_RATE,
                            value=str(gst_rate),
                            reason=f"GST rate for '{just_filled_value}' is typically {gst_rate}%.",
                            confidence=0.70,
                        ))

        # ── Supply type → CGST/SGST/IGST split ────────────────────────────
        if trigger_class == FieldClass.SUPPLY_TYPE:
            is_intra = any(kw in value_lower for kw in
                           ("intra", "local", "same state", "within"))
            is_inter = any(kw in value_lower for kw in
                           ("inter", "outside", "different state", "export"))

            gst_rate = None
            # Try to get current GST rate from session or context
            if FieldClass.TAX_RATE in self._session:
                try:
                    gst_rate = float(self._session[FieldClass.TAX_RATE])
                except ValueError:
                    pass
            if gst_rate is None:
                gst_rate = 18.0  # default

            for f in empty_fields:
                fc = classify(f.get("field_id",""), f.get("label",""))
                fid = f.get("field_id","")
                if is_intra:
                    if fc == FieldClass.CGST:
                        suggestions.append(Suggestion(
                            field_id=fid, field_class=fc,
                            value=str(gst_rate / 2),
                            reason=f"Intrastate supply: CGST = {gst_rate/2}% (half of {gst_rate}%).",
                            confidence=0.90,
                        ))
                    elif fc == FieldClass.SGST:
                        suggestions.append(Suggestion(
                            field_id=fid, field_class=fc,
                            value=str(gst_rate / 2),
                            reason=f"Intrastate supply: SGST = {gst_rate/2}% (half of {gst_rate}%).",
                            confidence=0.90,
                        ))
                    elif fc == FieldClass.IGST:
                        suggestions.append(Suggestion(
                            field_id=fid, field_class=fc,
                            value="0",
                            reason="IGST = 0 for intrastate supply.",
                            confidence=0.95,
                        ))
                elif is_inter:
                    if fc == FieldClass.IGST:
                        suggestions.append(Suggestion(
                            field_id=fid, field_class=fc,
                            value=str(gst_rate),
                            reason=f"Interstate supply: IGST = {gst_rate}%.",
                            confidence=0.90,
                        ))
                    elif fc == FieldClass.CGST:
                        suggestions.append(Suggestion(
                            field_id=fid, field_class=fc,
                            value="0",
                            reason="CGST = 0 for interstate supply.",
                            confidence=0.95,
                        ))
                    elif fc == FieldClass.SGST:
                        suggestions.append(Suggestion(
                            field_id=fid, field_class=fc,
                            value="0",
                            reason="SGST = 0 for interstate supply.",
                            confidence=0.95,
                        ))

        # ── Date → due date / delivery date ───────────────────────────────
        if trigger_class == FieldClass.DATE:
            filled_date = _parse_date(just_filled_value)
            if filled_date:
                for f in empty_fields:
                    fc = classify(f.get("field_id",""), f.get("label",""))
                    if fc == FieldClass.DUE_DATE:
                        due = filled_date + timedelta(days=30)
                        suggestions.append(Suggestion(
                            field_id=f.get("field_id",""),
                            field_class=fc,
                            value=due.strftime("%d/%m/%Y"),
                            reason="Due date set to 30 days from invoice date.",
                            confidence=0.65,
                        ))
                    elif fc == FieldClass.DELIVERY_DATE:
                        delivery = filled_date + timedelta(days=7)
                        suggestions.append(Suggestion(
                            field_id=f.get("field_id",""),
                            field_class=fc,
                            value=delivery.strftime("%d/%m/%Y"),
                            reason="Delivery date set to 7 days from invoice date.",
                            confidence=0.60,
                        ))

        # ── Supplier → payment mode from session memory ────────────────────
        if trigger_class == FieldClass.SUPPLIER:
            past = self._supplier_memory.get(just_filled_value, {})
            if FieldClass.PAYMENT_MODE in past:
                for f in empty_fields:
                    if classify(f.get("field_id",""), f.get("label","")) == FieldClass.PAYMENT_MODE:
                        suggestions.append(Suggestion(
                            field_id=f.get("field_id",""),
                            field_class=FieldClass.PAYMENT_MODE,
                            value=past[FieldClass.PAYMENT_MODE],
                            reason=f"Last time you used {past[FieldClass.PAYMENT_MODE]} for {just_filled_value}.",
                            confidence=0.80,
                        ))
                        break

        # Cap at 2 suggestions — don't overwhelm
        return suggestions[:2]

    def get_payment_modes(self) -> list[str]:
        return _PAYMENT_MODES

    def get_payment_terms(self) -> list[str]:
        return _PAYMENT_TERMS


# ── Helpers ───────────────────────────────────────────────────────────────

def _empty_fillable_fields(context: dict) -> list[dict]:
    return [
        f
        for s in context.get("sections", [])
        for f in s.get("fields", [])
        if not f.get("readonly") and not f.get("calculated")
        and not (f.get("value") and str(f["value"]).strip())
    ]


def _lookup_hsn(category_lower: str) -> tuple[str, str]:
    """Return (HSN code, description) or ("", "") if unknown."""
    for kw, (hsn, desc) in _CATEGORY_HSN.items():
        if kw in category_lower:
            return hsn, desc
    return "", ""


def _lookup_gst_rate(value_lower: str) -> int | None:
    """Return GST rate % for a category/product string, or None."""
    for kw, rate in _CATEGORY_GST.items():
        if kw in value_lower:
            return rate
    return None


def _parse_date(value: str) -> date | None:
    """Parse common date strings into a date object."""
    v = value.strip()
    if v.lower() in ("today", "aaj", "aaj ka"):
        return date.today()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return date.fromisoformat(v) if fmt == "%Y-%m-%d" else \
                   date(*[int(x) for x in re.split(r"[/\-]", v)][::-1]
                        if fmt.startswith("%d") else
                        [int(x) for x in re.split(r"[/\-]", v)])
        except Exception:
            continue
    return None


# ── Module singleton ──────────────────────────────────────────────────────
value_suggester = ValueSuggester()