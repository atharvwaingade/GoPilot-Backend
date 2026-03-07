"""
prompt_engine.py — Minimal prompts for chat API.

Since direct fill handles ~90% of cases, the LLM only sees
truly ambiguous instructions. Prompt is kept tiny for speed.
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

_INDIC = re.compile(r"[\u0900-\u0D7F]+")
_PARENS = re.compile(r"\([^)]+\)\s*")
_NOISE = {"fill","set","enter","put","as","to","the","a","an","with","value",
          "field","please","and","for","in","of","is","it","me","my","this"}


def _clean(t: str) -> str:
    return _INDIC.sub("", _PARENS.sub("", t)).strip().lower()


def _slug(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _clean(t)).strip("_")[:40]


def _keywords(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-zA-Z0-9]+", s.lower())
            if w not in _NOISE and len(w) > 1]


def _extract_value(instruction: str) -> str:
    m = re.search(r"\b(?:as|to|=|:)\s*(.+?)$", instruction.strip(), re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _build_compact_map(screen_context: dict, instruction: str) -> tuple[dict, str | None, str]:
    """
    Build label→field_id map. Returns (map, best_fid, best_value).
    Puts the most relevant field first.
    """
    kws = _keywords(instruction)
    value = _extract_value(instruction)

    entries: list[tuple[int, str, str]] = []  # (score, clean_label, fid)

    for section in screen_context.get("sections", []):
        for f in section.get("fields", []):
            fid = f.get("field_id", "")
            lbl = f.get("label", "")
            if not fid or not lbl or f.get("readonly") or f.get("calculated"):
                continue
            clean_lbl = _clean(lbl) or _clean(fid.replace("_", " "))
            if not clean_lbl:
                continue
            lbl_words = clean_lbl.split()

            # Gate: all keywords must appear somewhere in the label
            all_match = all(
                kw == clean_lbl or kw in lbl_words or kw in clean_lbl
                for kw in kws
            )
            if not all_match and len(kws) > 1:
                score = 0
            else:
                # Full phrase match gives large bonus
                query_phrase = " ".join(sorted(kws))
                label_phrase  = " ".join(sorted(lbl_words))
                phrase_bonus  = 50 if query_phrase == label_phrase else (
                                30 if clean_lbl.startswith(" ".join(kws)) else 0)
                per_tok = sum(
                    10 if kw == clean_lbl else
                    8  if kw in lbl_words else
                    3  if kw in clean_lbl else 0
                    for kw in kws
                )
                # Penalise extra words (product description > product name for "product name")
                unmatched = sum(1 for w in lbl_words if w not in kws)
                precision_penalty = unmatched * 2
                # Exact word count match bonus
                length_bonus = 10 if len(lbl_words) == len(kws) else 0
                score = max(0, phrase_bonus + per_tok - precision_penalty + length_bonus)

            entries.append((score, clean_lbl, fid))

    entries.sort(key=lambda x: -x[0])

    # Build map with best match first, cap at 25 entries
    label_map: dict[str, str] = {}
    for _, lbl, fid in entries[:25]:
        if lbl not in label_map:
            label_map[lbl] = fid

    best_fid = entries[0][2] if entries and entries[0][0] > 0 else None
    return label_map, best_fid, value


def build_planning_prompt(
    workflow_name: str,
    screen_context: dict,
    user_instruction: str,
    next_field: str | None,
    calculated_fields: list[str],
    readonly_fields: list[str],
    required_fields: list[str],
    missing_required: list[str],
) -> str:
    label_map, best_fid, value = _build_compact_map(screen_context, user_instruction)

    map_json = json.dumps(label_map, separators=(",", ":"))
    if len(map_json) > 2000:
        # Keep only first 20 entries
        map_json = json.dumps(dict(list(label_map.items())[:20]), separators=(",", ":"))

    lines = [f"FIELDS:{map_json}"]

    # Give model a concrete expected output when we have a best match
    if best_fid and value:
        lines.append(
            f"HINT: field_id for this request is likely \"{best_fid}\""
        )
        lines.append(
            f"EXPECTED:{json.dumps({'action':'tool_call','field_id':best_fid,'value':value,'reason':'user instruction'}, separators=(',',':'))}"
        )

    lines.append(f"TASK:{user_instruction.strip()}")
    lines.append("JSON:")
    return "\n".join(lines)


def build_retry_prompt(original: str, bad_output: str, error: str) -> str:
    # Extract fields and task from original
    fields_match = re.search(r"FIELDS:(\{.+?\})", original, re.DOTALL)
    task_match   = re.search(r"TASK:(.+?)(?:\n|$)", original)
    hint_match   = re.search(r"EXPECTED:(.+?)(?:\n|$)", original)

    fields = fields_match.group(1) if fields_match else "{}"
    task   = task_match.group(1).strip() if task_match else ""
    hint   = hint_match.group(1).strip() if hint_match else ""

    lines = [
        f"FIELDS:{fields}",
        f"TASK:{task}",
        f"WRONG:{bad_output[:80]}",
        f"ERROR:{error[:100]}",
    ]
    if hint:
        lines.append(f"CORRECT OUTPUT IS:{hint}")
    lines.append("JSON:")
    return "\n".join(lines)


def extract_live_metadata(screen_context: dict) -> dict:
    req, calc, ro, all_f = [], [], [], []
    for section in screen_context.get("sections", []):
        for f in section.get("fields", []):
            fid = f.get("field_id")
            if not fid:
                continue
            all_f.append(fid)
            if f.get("required"):   req.append(fid)
            if f.get("calculated"): calc.append(fid)
            if f.get("readonly"):   ro.append(fid)
    return {"required_fields": req, "calculated_fields": calc,
            "readonly_fields": ro, "execution_order": all_f}