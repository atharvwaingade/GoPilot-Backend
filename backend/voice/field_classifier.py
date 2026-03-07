"""
field_classifier.py — Universal semantic field classifier (Stage 3.1)

Classifies any form field into a FieldClass using ONLY:
  - field_id (slug)
  - label text (strips Indic script / parentheticals)
  - HTML input type
  - placeholder text

Zero LLM calls. <1ms per field.

Works on: ERP (Purchase/Sales/Supplier/Customer), Google Forms, Typeform,
          Shopify, WooCommerce, Salesforce, HR portals, logistics dashboards,
          any admin panel in English/Hindi/Marathi.

Powers:
  - Proactive suggestions ("I notice a barcode field — want me to generate one?")
  - value_suggester.py (predictive HSN codes, payment modes, etc.)
  - Real-time validation (quantity=0, future date warnings)
  - Auto-fill of tax rates from GST rules
"""
from __future__ import annotations

import re
from enum import Enum


class FieldClass(str, Enum):
    # ── Product / Commerce ────────────────────────────────────────────────
    PRODUCT_NAME   = "product_name"
    PRODUCT_CODE   = "product_code"
    CATEGORY       = "category"
    BARCODE        = "barcode"
    BRAND          = "brand"
    SKU            = "sku"

    # ── Pricing / Finance ─────────────────────────────────────────────────
    PRICE          = "price"
    QUANTITY       = "quantity"
    DISCOUNT       = "discount"
    TOTAL          = "total"
    TAX_RATE       = "tax_rate"
    TAX_AMOUNT     = "tax_amount"
    CURRENCY       = "currency"
    PAYMENT_MODE   = "payment_mode"

    # ── GST / India-specific ──────────────────────────────────────────────
    HSN_SAC        = "hsn_sac"
    GSTIN          = "gstin"
    CGST           = "cgst"
    SGST           = "sgst"
    IGST           = "igst"
    SUPPLY_TYPE    = "supply_type"

    # ── Documents / References ────────────────────────────────────────────
    INVOICE_NO     = "invoice_no"
    ORDER_NO       = "order_no"
    PO_NUMBER      = "po_number"
    REFERENCE_NO   = "reference_no"

    # ── Party / Contact ───────────────────────────────────────────────────
    SUPPLIER       = "supplier"
    CUSTOMER       = "customer"
    EMAIL          = "email"
    PHONE          = "phone"
    ADDRESS        = "address"
    CITY           = "city"
    STATE          = "state"
    PINCODE        = "pincode"
    COUNTRY        = "country"
    COMPANY        = "company"

    # ── People / HR ───────────────────────────────────────────────────────
    PERSON_NAME    = "person_name"
    FIRST_NAME     = "first_name"
    LAST_NAME      = "last_name"
    EMPLOYEE_ID    = "employee_id"
    DEPARTMENT     = "department"

    # ── Dates ─────────────────────────────────────────────────────────────
    DATE           = "date"
    DATE_FROM      = "date_from"
    DATE_TO        = "date_to"
    DUE_DATE       = "due_date"
    DELIVERY_DATE  = "delivery_date"

    # ── Logistics ─────────────────────────────────────────────────────────
    WEIGHT         = "weight"
    TRACKING_NO    = "tracking_no"
    CARRIER        = "carrier"
    WAREHOUSE      = "warehouse"

    # ── Auth ──────────────────────────────────────────────────────────────
    USERNAME       = "username"
    PASSWORD       = "password"
    CONFIRM_PASS   = "confirm_password"

    # ── Content / General ─────────────────────────────────────────────────
    DESCRIPTION    = "description"
    NOTES          = "notes"
    STATUS         = "status"
    TAGS           = "tags"
    URL            = "url"
    FILE_UPLOAD    = "file_upload"
    RATING         = "rating"
    CHECKBOX       = "checkbox"
    RADIO          = "radio"
    SUBMIT         = "submit"

    UNKNOWN        = "unknown"


# ── Pattern table ─────────────────────────────────────────────────────────
# Checked top-to-bottom; first hit wins.
# Each entry: (FieldClass, [substrings that match the normalised slug])
_PATTERNS: list[tuple[FieldClass, list[str]]] = [

    # Auth — check first (password clashes with other surfaces)
    (FieldClass.PASSWORD,      ["password", "passwd", "pwd"]),
    (FieldClass.CONFIRM_PASS,  ["confirm_pass", "retype_pass", "repeat_pass",
                                 "password_confirm", "new_password"]),
    (FieldClass.USERNAME,      ["username", "user_name", "login_id", "login",
                                 "signin", "user_id"]),

    # GST / India
    (FieldClass.HSN_SAC,       ["hsn", "sac_code", "hsn_sac", "hsncode"]),
    (FieldClass.GSTIN,         ["gstin", "gst_no", "gst_number", "gst_reg"]),
    (FieldClass.CGST,          ["cgst"]),
    (FieldClass.SGST,          ["sgst"]),
    (FieldClass.IGST,          ["igst"]),
    (FieldClass.SUPPLY_TYPE,   ["supply_type", "intrastate", "interstate",
                                 "gst_type"]),
    (FieldClass.TAX_RATE,      ["tax_rate", "tax_percent", "gst_rate", "vat_rate",
                                 "tax_slab", "rate_of_tax"]),
    (FieldClass.TAX_AMOUNT,    ["tax_amount", "tax_value", "gst_amount",
                                 "cgst_amount", "sgst_amount", "igst_amount"]),

    # Barcode / SKU / Product codes
    (FieldClass.BARCODE,       ["barcode", "bar_code", "ean", "upc", "qr_code"]),
    (FieldClass.SKU,           ["sku", "stock_keeping", "item_sku"]),
    (FieldClass.PRODUCT_CODE,  ["product_code", "prod_code", "item_code",
                                 "article_no", "part_no", "item_no"]),

    # Document numbers
    (FieldClass.INVOICE_NO,    ["invoice_no", "invoice_num", "invoice_number",
                                 "inv_no", "bill_no", "bill_number"]),
    (FieldClass.ORDER_NO,      ["order_no", "order_num", "order_number",
                                 "order_id", "so_no", "sales_order"]),
    (FieldClass.PO_NUMBER,     ["po_no", "po_number", "purchase_order_no",
                                 "po_num", "porder"]),
    (FieldClass.REFERENCE_NO,  ["ref_no", "reference_no", "ref_num",
                                 "reference_number", "ref_id"]),

    # Dates — specific before generic
    (FieldClass.DUE_DATE,      ["due_date", "payment_due", "due_by"]),
    (FieldClass.DELIVERY_DATE, ["delivery_date", "dispatch_date", "ship_date",
                                 "expected_date", "eta"]),
    (FieldClass.DATE_FROM,     ["date_from", "from_date", "start_date",
                                 "begin_date", "valid_from"]),
    (FieldClass.DATE_TO,       ["date_to", "to_date", "end_date",
                                 "valid_to", "expiry_date", "expire"]),
    (FieldClass.DATE,          ["tarikh", "tdate", "_date", "date"]),

    # Pricing / Finance
    (FieldClass.TOTAL,         ["grand_total", "total_amount", "net_amount",
                                 "payable", "to_pay", "net_pay", "final_amount"]),
    (FieldClass.DISCOUNT,      ["discount", "rebate", "deduction"]),
    (FieldClass.PRICE,         ["unit_price", "sell_price", "mrp", "selling_price",
                                 "rate_per_unit", "price", "cost", "rate"]),
    (FieldClass.QUANTITY,      ["quantity", "qty", "total_qty", "units",
                                 "no_of", "number_of", "pieces"]),
    (FieldClass.PAYMENT_MODE,  ["payment_mode", "pay_mode", "mode_of_payment",
                                 "payment_method", "pay_by"]),
    (FieldClass.CURRENCY,      ["currency", "curr", "denomination"]),

    # Parties
    (FieldClass.SUPPLIER,      ["supplier", "vendor", "aapurtikarta",
                                 "purvatakar", "party_name", "seller"]),
    (FieldClass.CUSTOMER,      ["customer", "client", "grahak", "buyer"]),
    (FieldClass.EMAIL,         ["email", "e_mail", "mail_id"]),
    (FieldClass.PHONE,         ["phone", "mobile", "contact_no", "tel",
                                 "cell", "whatsapp"]),
    (FieldClass.ADDRESS,       ["address", "addr", "street"]),
    (FieldClass.PINCODE,       ["pincode", "pin_code", "zip", "postal_code"]),
    (FieldClass.CITY,          ["city", "town", "district"]),
    (FieldClass.STATE,         ["state", "province", "region"]),
    (FieldClass.COUNTRY,       ["country", "nation"]),
    (FieldClass.COMPANY,       ["company", "organisation", "organization",
                                 "firm", "business_name"]),

    # HR / People
    (FieldClass.FIRST_NAME,    ["first_name", "fname", "given_name"]),
    (FieldClass.LAST_NAME,     ["last_name", "lname", "surname"]),
    (FieldClass.EMPLOYEE_ID,   ["employee_id", "emp_id", "staff_id", "emp_no"]),
    (FieldClass.DEPARTMENT,    ["department", "dept", "division"]),

    # Logistics
    (FieldClass.TRACKING_NO,   ["tracking", "track_no", "awb", "consignment",
                                 "shipment_id", "docket"]),
    (FieldClass.CARRIER,       ["carrier", "courier", "shipper", "transport"]),
    (FieldClass.WAREHOUSE,     ["warehouse", "godown", "depot", "location_code"]),
    (FieldClass.WEIGHT,        ["weight", "net_weight", "gross_weight"]),

    # Product / category
    (FieldClass.CATEGORY,      ["category", "shreni", "prakar", "product_type",
                                 "item_type", "group", "segment"]),
    (FieldClass.BRAND,         ["brand", "make", "manufacturer"]),
    (FieldClass.PRODUCT_NAME,  ["product_name", "prod_name", "item_name",
                                 "product_title"]),
    (FieldClass.DESCRIPTION,   ["description", "desc", "details", "remarks",
                                 "narration"]),
    (FieldClass.STATUS,        ["status", "condition", "stage"]),
    (FieldClass.TAGS,          ["tags", "labels", "keywords"]),
    (FieldClass.URL,           ["url", "link", "website"]),
    (FieldClass.FILE_UPLOAD,   ["file", "upload", "attachment"]),
    (FieldClass.RATING,        ["rating", "score", "stars"]),
    (FieldClass.NOTES,         ["note", "remark", "additional_info",
                                 "extra_details"]),

    # Person name (generic — after first/last)
    (FieldClass.PERSON_NAME,   ["full_name", "your_name", "person_name",
                                 "contact_name", "applicant_name", "name"]),

    # Form controls
    (FieldClass.SUBMIT,        ["submit", "save", "confirm", "finish", "send"]),
    (FieldClass.CHECKBOX,      ["agree", "accept", "consent", "terms",
                                 "newsletter", "subscribe"]),
]

# ── Field behaviour sets ──────────────────────────────────────────────────

# Fields CoPilot proactively offers help for when empty
PROACTIVE_CLASSES: set[FieldClass] = {
    FieldClass.BARCODE,
    FieldClass.HSN_SAC,
    FieldClass.GSTIN,
    FieldClass.DATE,
    FieldClass.DUE_DATE,
    FieldClass.DELIVERY_DATE,
    FieldClass.TAX_RATE,
    FieldClass.CGST,
    FieldClass.SGST,
    FieldClass.IGST,
    FieldClass.TOTAL,
}

# Fields that should trigger real-time validation warnings
VALIDATE_CLASSES: set[FieldClass] = {
    FieldClass.QUANTITY,
    FieldClass.PRICE,
    FieldClass.DATE,
    FieldClass.DUE_DATE,
    FieldClass.DELIVERY_DATE,
    FieldClass.GSTIN,
    FieldClass.EMAIL,
    FieldClass.PHONE,
    FieldClass.PINCODE,
}

# Proactive hint templates per class
_HINT_TEMPLATES: dict[FieldClass, str] = {
    FieldClass.BARCODE:
        "I notice there's a barcode field — want me to generate one?",
    FieldClass.HSN_SAC:
        "The HSN code field is empty — I can suggest one based on the category.",
    FieldClass.GSTIN:
        "There's a GSTIN field — I'll validate the format when you fill it.",
    FieldClass.TAX_RATE:
        "I can auto-fill the GST rate once you've set the supply type.",
    FieldClass.CGST:
        "CGST can be calculated automatically — say 'calculate tax' when ready.",
    FieldClass.SGST:
        "SGST can be calculated automatically from the tax rate.",
    FieldClass.IGST:
        "IGST applies for inter-state supply — I can fill this automatically.",
    FieldClass.TOTAL:
        "I can calculate the total automatically once all items are filled.",
    FieldClass.DATE:
        "The date field is empty — say 'today' or give me a date.",
    FieldClass.DUE_DATE:
        "There's a due date field — want me to set it to 30 days from today?",
    FieldClass.DELIVERY_DATE:
        "A delivery date is needed — want me to suggest one?",
}


def _slug(text: str) -> str:
    """Normalise a label or field_id to a lowercase slug for matching."""
    t = re.sub(r"\([^)]*\)", "", text)                    # strip (parentheticals)
    t = re.sub(r"[\u0900-\u0D7F\u0A00-\u0A7F]+", "", t)  # strip Indic scripts
    t = re.sub(r"[^a-z0-9]+", "_", t.lower())
    return t.strip("_")


def classify(
    field_id:    str,
    label:       str = "",
    input_type:  str = "text",
    placeholder: str = "",
) -> FieldClass:
    """
    Classify a single form field into a FieldClass.

    Checks patterns against normalised slugs of field_id, label, placeholder.
    HTML input type shortcuts run first. First match wins.

    Examples:
        classify("enter_product_category")    → FieldClass.CATEGORY
        classify("cgst_percent")              → FieldClass.CGST
        classify("barcode_upc")               → FieldClass.BARCODE
        classify("f1", label="HSN/SAC Code")  → FieldClass.HSN_SAC
        classify("x", input_type="email")     → FieldClass.EMAIL
    """
    # HTML type fast-path
    _TYPE_MAP: dict[str, FieldClass] = {
        "email":    FieldClass.EMAIL,
        "tel":      FieldClass.PHONE,
        "date":     FieldClass.DATE,
        "password": FieldClass.PASSWORD,
        "file":     FieldClass.FILE_UPLOAD,
        "checkbox": FieldClass.CHECKBOX,
        "radio":    FieldClass.RADIO,
        "url":      FieldClass.URL,
    }
    if input_type in _TYPE_MAP:
        return _TYPE_MAP[input_type]

    surfaces = [_slug(field_id), _slug(label), _slug(placeholder)]

    for field_class, patterns in _PATTERNS:
        for pat in patterns:
            pat_slug = _slug(pat)
            for surface in surfaces:
                if surface and (pat_slug in surface or surface == pat_slug):
                    return field_class

    return FieldClass.UNKNOWN


def classify_context(context: dict) -> dict[str, FieldClass]:
    """
    Classify all fields in a screen context dict at once.
    Returns {field_id: FieldClass}.
    """
    result: dict[str, FieldClass] = {}
    for section in context.get("sections", []):
        for f in section.get("fields", []):
            fid = f.get("field_id", "")
            if fid:
                result[fid] = classify(
                    field_id=fid,
                    label=f.get("label", ""),
                    input_type=f.get("type", "text"),
                    placeholder=f.get("placeholder", "") or "",
                )
    return result


def proactive_hints(context: dict) -> list[str]:
    """
    Return up to 3 spoken proactive hints for empty PROACTIVE_CLASSES fields.

    E.g. ["I notice there's a barcode field — want me to generate one?",
          "The HSN code field is empty — I can suggest one from the category."]
    """
    hints: list[str] = []
    classifications = classify_context(context)

    for fid, fc in classifications.items():
        if fc not in PROACTIVE_CLASSES:
            continue
        for section in context.get("sections", []):
            for f in section.get("fields", []):
                if (f.get("field_id") == fid
                        and not f.get("readonly")
                        and not (f.get("value") and str(f["value"]).strip())):
                    hint = _HINT_TEMPLATES.get(fc)
                    if hint and hint not in hints:
                        hints.append(hint)

    return hints[:3]