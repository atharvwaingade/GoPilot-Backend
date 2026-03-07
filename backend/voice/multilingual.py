"""
multilingual.py — Hindi + Marathi + English intent normaliser

The STT engine (Whisper auto mode) correctly transcribes code-switched speech.
This module handles what comes OUT of Whisper — mapping Devanagari phrases and
Romanised Hindi/Marathi words to English equivalents so the controller can
match them against field names and intent patterns.

Examples of what this handles:
  "category ko chairs se bharo"     → "fill category as chairs"
  "श्रेणी में कुर्सियाँ भरो"          → "fill category as chairs"
  "submit karo"                      → "submit"
  "naam kya hai"                     → "what is name"
  "required fields bharo"            → "fill required fields"
  "मला समजावून सांग"                  → "explain this page"
  "band karo"                        → "stop"
"""
from __future__ import annotations

import re
import logging

logger = logging.getLogger(__name__)

# ── Hindi/Marathi fill-intent remappings ──────────────────────────────────────
# Each tuple: (pattern, replacement) — applied in order, case-insensitive
_REMAP_RULES: list[tuple[re.Pattern, str]] = [
    # ── Navigation remapping ──────────────────────────────────────────────
    # "गो तु परचेस" → "go to purchase" / "ओपन सप्लायर" → "open supplier"
    (re.compile(r"(?:go|गो)\s+(?:tu|to|तु|तो)\s+(.+)", re.I | re.U),  r"go to \1"),
    (re.compile(r"(?:open|ओपन|khol|खोल)\s+(.+)",          re.I | re.U),  r"open \1"),
    (re.compile(r"(?:wapas|वापस|back)\s*(?:jao|जाओ|ja|जा)?", re.I | re.U), "go back"),
    (re.compile(r"(?:ghar|घर)",                         re.I | re.U), "go home"),
    # Transliterated page names → English
    (re.compile(r"parchas|parches|purchas|perchase", re.I), "purchase"),
    (re.compile(r"saplayer|supplyer|sapplyar",            re.I), "supplier"),
    (re.compile(r"kastomar|customar|kastmer",             re.I), "customer"),
    (re.compile(r"seals|sale(?!\s+order)",                  re.I), "sales"),

    # ── Hindi fill verbs ──────────────────────────────────────────────────
    # "X ko Y se bharo" / "X mein Y likho" → "fill X as Y"
    (re.compile(r"(\w+)\s+ko\s+(.+?)\s+(se\s+)?bharo?", re.I),
     r"fill \1 as \2"),
    (re.compile(r"(\w+)\s+mein\s+(.+?)\s+(likho?|dalo?|bharo?)", re.I),
     r"fill \1 as \2"),
    (re.compile(r"(\w+)\s+(?:set|daalo?)\s+karo?\s+(.+)", re.I),
     r"fill \1 as \2"),

    # ── Marathi fill verbs ─────────────────────────────────────────────────
    # "X madhe Y ghala" / "X la Y kar"
    (re.compile(r"(\w+)\s+madhe\s+(.+?)\s+ghala?", re.I),
     r"fill \1 as \2"),
    (re.compile(r"(\w+)\s+la\s+(.+?)\s+kar", re.I),
     r"fill \1 as \2"),

    # ── Submit / save ──────────────────────────────────────────────────────
    (re.compile(r"\b(submit|save|jama|saave|jama\s+karo?|save\s+karo?|"
                r"bhejo?|send\s+karo?|सेव करो|जमा करो)\b", re.I),
     "submit"),

    # ── Cancel / stop ─────────────────────────────────────────────────────
    (re.compile(r"\b(band\s+karo?|roko?|cancel\s+karo?|nahi|nahin|"
                r"रोको|बंद करो)\b", re.I),
     "cancel"),

    # ── List / show fields ─────────────────────────────────────────────────
    (re.compile(r"\b(konse|kaunse|kaun\s+se|कौन\s*से|कोणते)\s+(fields?|"
                r"column|input)", re.I),
     "what fields"),
    (re.compile(r"\b(fields?\s+dikhaao?|form\s+dikhaao?|सभी\s+फील्ड)\b", re.I),
     "list fields"),

    # ── Explain / what is ─────────────────────────────────────────────────
    (re.compile(r"\b(kya\s+hai|क्या\s+है|काय\s+आहे|samjhao?|समझाओ|"
                r"batao?|बताओ|mala\s+samjav)\b", re.I),
     "what is"),
    (re.compile(r"\b(samjhao?\s+(?:yeh|ye|this|is)|mujhe\s+samjhao?|"
                r"समझाओ|मला\s+समजावून\s+सांग)\b", re.I),
     "explain this page"),

    # ── Required fields ───────────────────────────────────────────────────
    (re.compile(r"\b(required\s+fields?\s+(?:bharo?|fill)|"
                r"sabhi?\s+required|आवश्यक\s+फील्ड)\b", re.I),
     "fill required fields"),

    # ── Undo ──────────────────────────────────────────────────────────────
    (re.compile(r"\b(pichla?\s+(?:hatao?|undo)|वापस\s+करो?|undo\s+karo?)\b",
                re.I),
     "undo"),

    # ── Devanagari digit normalisation ────────────────────────────────────
    # ०१२३४५६७८९ → 0123456789
]

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Marathi/Hindi common field-name translations → English slug
_FIELD_NAME_MAP: dict[str, str] = {
    # Hindi
    "naam":        "name",
    "नाम":         "name",
    "नाव":         "name",           # Marathi
    "shreni":      "category",
    "श्रेणी":      "category",
    "prakar":      "category",
    "प्रकार":      "category",
    "tarikh":      "date",
    "तारीख":       "date",
    "mool":        "rate",
    "mulya":       "price",
    "किंमत":       "price",
    "matra":       "quantity",
    "maatra":      "quantity",
    "मात्रा":      "quantity",
    "purvatakar":  "supplier",
    "aapurtikarta":"supplier",
    "ग्राहक":      "customer",
    "grahak":      "customer",
    "invoice":     "invoice",
    "pawan":       "invoice",
    "rasid":       "invoice",
    # ── Devanagari page/section names ────────────────────────────────────
    "परचेस":        "purchase",
    "खरेदी":        "purchase",
    "विक्री":       "sales",
    "सप्लायर":      "supplier",
    "ग्राहक":       "customer",
    "डैशबोर्ड":     "dashboard",
    "स्टॉक":        "stock",
    "इन्वेंटरी":    "inventory",
}


def normalise(text: str) -> str:
    """
    Normalise a Whisper transcription for intent matching.

    1. Devanagari digit → ASCII digit
    2. Known Hindi/Marathi field names → English slugs
    3. Fill-intent remapping rules

    Returns the normalised English-dominant string.
    The original text is preserved if no rule fires (safe to call always).
    """
    if not text:
        return text

    result = text.translate(_DEVANAGARI_DIGITS)

    # Field name substitution (word boundary)
    for native, english in _FIELD_NAME_MAP.items():
        result = re.sub(
            rf"\b{re.escape(native)}\b", english, result, flags=re.IGNORECASE | re.UNICODE
        )

    # Intent remapping rules
    for pattern, replacement in _REMAP_RULES:
        new = pattern.sub(replacement, result)
        if new != result:
            logger.debug("multilingual.normalise: '%s' → '%s'", result[:60], new[:60])
            result = new
            break   # apply at most one fill-verb rule per pass

    return result.strip()