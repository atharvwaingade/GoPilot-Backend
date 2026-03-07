"""
sentence_parser.py — Natural sentence multi-slot extraction (Tier 1.5)

Sits between direct fill (Tier 1) and the LLM (Tier 3).
Handles one-sentence commands like:
  "purchase for 50 chairs from ABC today"
  "create purchase 100 tables supplier Raju date today"
  "50 sofas from XYZ Traders at 200"
  "25 chairs from ABC Traders on 15 jan 2024"

Returns a list of (field_id, value) tuples matched against the live DOM,
or [] if the sentence doesn't look like a multi-slot command.

Zero LLM calls. <2ms.

Algorithm
─────────
1. Run slot extractors in priority order (labeled slots first)
2. Each extractor claims a portion of the sentence and removes it
3. Map slot types → actual field_ids using the DOM field scorer
4. Return only slots with confident field matches (score ≥ 3)

Slot types extracted
────────────────────
  quantity   — "50", "qty 50", "100 pieces"
  category   — "chairs", "tables", "item chairs"
  supplier   — "from ABC Traders", "supplier Raju", "vendor XYZ"
  date       — "today", "tomorrow", "on 15 jan", "dated 01/02/2024"
  price      — "at 200", "price 150", "rate 75"
  customer   — "customer Raju Traders", "client XYZ"
  name       — "name John", leading proper noun in non-purchase context
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Words that are form/command noise — not field values
_NOISE = {
    "purchase", "order", "buy", "create", "new", "add", "make", "record",
    "fill", "set", "enter", "put", "a", "an", "the", "some", "for", "please",
    "me", "my", "this", "that", "and", "also", "then", "now", "just",
}

# Slot type → common field keywords (used for DOM field matching)
_SLOT_FIELD_KEYWORDS: dict[str, list[str]] = {
    "quantity": ["quantity", "qty", "count", "number", "nos", "pieces", "units", "amount"],
    "category": ["category", "type", "item", "product", "goods", "material"],
    "supplier": ["supplier", "vendor", "party", "from", "seller", "creditor"],
    "customer": ["customer", "client", "buyer", "purchaser", "to", "debtor"],
    "date":     ["date", "invoice", "bill", "on", "dated"],
    "price":    ["price", "rate", "cost", "unit", "per", "amount"],
    "name":     ["name", "title", "description"],
}


# ── Slot extractor ────────────────────────────────────────────────────────────

def extract_slots(text: str) -> dict[str, str]:
    """
    Extract semantic slots from a natural voice sentence.

    Returns dict of slot_type → raw_value, e.g.:
      {"quantity": "50", "category": "chairs", "supplier": "ABC", "date": "today"}
    """
    slots: dict[str, str] = {}
    rem = text.strip()

    # ── Priority 1: Explicitly labeled slots ─────────────────────────────────
    # "qty N" / "quantity N"
    m = re.search(r'\b(?:qty|quantity|qnty|count|nos?)\s+(\d+(?:\.\d+)?)', rem, re.I)
    if m:
        slots['quantity'] = m.group(1)
        rem = rem[:m.start()] + " " + rem[m.end():]

    # "supplier/vendor/party NAME"
    m = re.search(
        r'\b(?:supplier|vendor|party)\s+([A-Za-z][A-Za-z0-9\s&\.]{1,35}?)'
        r'(?=\s+(?:qty|price|date|today|tomorrow|on|\d)|$)',
        rem, re.I)
    if m:
        slots['supplier'] = m.group(1).strip()
        rem = rem[:m.start()] + " " + rem[m.end():]

    # "customer/client NAME"
    m = re.search(
        r'\b(?:customer|client|buyer)\s+([A-Za-z][A-Za-z0-9\s&\.]{1,35}?)'
        r'(?=\s+(?:qty|price|date|today|tomorrow|on|\d)|$)',
        rem, re.I)
    if m:
        slots['customer'] = m.group(1).strip()
        rem = rem[:m.start()] + " " + rem[m.end():]

    # "price/rate/cost N"
    m = re.search(r'\b(?:price|rate|cost)\s+(\d+(?:\.\d+)?)', rem, re.I)
    if m:
        slots['price'] = m.group(1)
        rem = rem[:m.start()] + " " + rem[m.end():]

    # "category/type/item NAME"
    m = re.search(
        r'\b(?:category|type|item)\s+([A-Za-z][A-Za-z0-9\s]{1,30}?)'
        r'(?=\s+(?:from|supplier|vendor|qty|price|date|today|\d)|$)',
        rem, re.I)
    if m:
        slots['category'] = m.group(1).strip()
        rem = rem[:m.start()] + " " + rem[m.end():]

    # "on/dated DATE" (explicit date format)
    m = re.search(
        r'\b(?:dated?|on)\s+'
        r'(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?'
        r'|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)'
        r'[a-z]*(?:\s+\d{2,4})?)',
        rem, re.I)
    if m:
        slots['date'] = m.group(1).strip()
        rem = rem[:m.start()] + " " + rem[m.end():]

    # "today / tomorrow / yesterday"
    m = re.search(r'\b(today|tomorrow|yesterday)\b', rem, re.I)
    if m:
        if 'date' not in slots:
            slots['date'] = m.group(1).lower()
        rem = rem[:m.start()] + " " + rem[m.end():]

    # ── Priority 2: Positional slots ─────────────────────────────────────────
    # "from NAME" supplier
    if 'supplier' not in slots:
        m = re.search(
            r'\bfrom\s+([A-Za-z][A-Za-z0-9\s&\.]{1,35}?)'
            r'(?=\s+(?:at|price|date|today|tomorrow|qty|\d|on|for)|$)',
            rem, re.I)
        if m:
            slots['supplier'] = m.group(1).strip()
            rem = rem[:m.start()] + " " + rem[m.end():]

    # "at N" or "@ N" — price
    if 'price' not in slots:
        m = re.search(r'\b(?:at|@)\s+(\d+(?:\.\d+)?)', rem, re.I)
        if m:
            slots['price'] = m.group(1)
            rem = rem[:m.start()] + " " + rem[m.end():]

    # ── Priority 3: Number + noun = qty + category ────────────────────────────
    if 'quantity' not in slots or 'category' not in slots:
        m = re.search(
            r'\b(\d+(?:\.\d+)?)\s*'
            r'(?:pieces?|pcs?|units?|kg|ltr?|litres?|dozen|boxes?|bags?|nos?)?\s+'
            r'([a-zA-Z][a-zA-Z\s]{1,25}?)'
            r'(?=\s+(?:from|supplier|vendor|at|price|date|today|tomorrow|on)|$)',
            rem, re.I)
        if m:
            if 'quantity' not in slots:
                slots['quantity'] = m.group(1)
            if 'category' not in slots:
                cat = m.group(2).strip()
                cat = re.sub(r'\s+(?:from|for|at|and|in|on)$', '', cat, flags=re.I).strip()
                if cat and len(cat) > 1 and cat.lower() not in _NOISE:
                    slots['category'] = cat
            rem = rem[:m.start()] + " " + rem[m.end():]

    # ── Priority 4: Leading noun before "from" ─────────────────────────────
    if 'category' not in slots:
        m = re.match(r'^[\s\w]*?\s*([a-zA-Z][a-zA-Z\s]{1,20}?)\s+from\b', rem.strip(), re.I)
        if m:
            candidate = m.group(1).strip().lower()
            words = candidate.split()
            clean = ' '.join(w for w in words if w not in _NOISE)
            if clean and len(clean) > 1:
                slots['category'] = clean

    # Strip trailing punctuation/whitespace from all values (STT adds sentence-end periods)
    slots = {k: v.strip().rstrip(".,!? ") for k, v in slots.items()}
    return slots


# ── Field mapper ──────────────────────────────────────────────────────────────

def _score_field_for_slot(field: dict, slot_type: str) -> int:
    """Score how well a DOM field matches a given slot type."""
    fid   = field.get("field_id", "").lower()
    label = field.get("label", "").lower()
    label = re.sub(r"[^\x00-\x7F]", "", label)   # strip Indic chars
    label = re.sub(r"[^a-z0-9\s]", " ", label).strip()
    ph    = (field.get("placeholder", "") or "").lower()

    keywords = _SLOT_FIELD_KEYWORDS.get(slot_type, [])
    score = 0

    # Score against field surfaces
    for kw in keywords:
        for surface in (fid, label, ph):
            if not surface:
                continue
            words = surface.split()
            if kw == surface.strip():   score += 20
            elif kw in words:           score += 10
            elif surface.startswith(kw): score += 6
            elif kw in surface:         score += 3

    return score


def map_slots_to_fields(
    slots: dict[str, str],
    screen_context: dict,
) -> list[tuple[str, str, str]]:
    """
    Map extracted slots to actual DOM field_ids.

    Returns list of (field_id, label, value) tuples, sorted by DOM order.
    Only returns slots where a field match was found with confidence ≥ 3.
    """
    all_fields = [
        f for s in screen_context.get("sections", [])
        for f in s.get("fields", [])
        if not f.get("readonly") and not f.get("calculated") and f.get("field_id")
    ]

    results: list[tuple[int, str, str, str]] = []  # (dom_order, field_id, label, value)
    used_field_ids: set[str] = set()

    for slot_type, value in slots.items():
        best_score  = 0
        best_field  = None
        best_order  = 0

        for order, field in enumerate(all_fields):
            fid = field.get("field_id", "")
            if fid in used_field_ids:
                continue
            s = _score_field_for_slot(field, slot_type)
            if s > best_score:
                best_score  = s
                best_field  = field
                best_order  = order

        if best_field and best_score >= 3:
            fid   = best_field.get("field_id", "")
            label = best_field.get("label", "")
            used_field_ids.add(fid)
            results.append((best_order, fid, label, value))
            logger.debug("Slot '%s'='%s' → field_id='%s' (score=%d)",
                         slot_type, value, fid, best_score)
        else:
            logger.debug("Slot '%s'='%s' → no confident field match (best=%d)",
                         slot_type, value, best_score)

    # Return in DOM order
    results.sort(key=lambda x: x[0])
    return [(fid, label, value) for _, fid, label, value in results]


# ── Guard: is this a multi-slot sentence? ────────────────────────────────────

# These are handled by Tier 1 direct fill — don't parse them here
_DIRECT_FILL_RE = re.compile(
    r"^(?:fill|set|enter|put|type|write|update|change|make)\s+.+\s+(?:as|to|=|:)\s+.+$",
    re.I,
)

# Words that strongly suggest this is a purchase/sales natural sentence
_PURCHASE_SIGNALS = re.compile(
    r"\b(?:purchase|order|buy|create|new order|add purchase|sale|selling|"
    r"from\s+[A-Z]|supplier|vendor|qty|quantity)\b",
    re.I,
)


def is_multi_slot_sentence(instruction: str) -> bool:
    """
    Returns True if instruction looks like a natural multi-slot sentence
    rather than a single direct fill command.
    """
    # Tier 1 handles "fill X as Y" exactly — don't intercept
    if _DIRECT_FILL_RE.match(instruction.strip()):
        return False

    slots = extract_slots(instruction)

    # Need at least 2 slots to be worth multi-fill
    return len(slots) >= 2