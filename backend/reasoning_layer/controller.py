"""
controller.py — Universal ReasoningController

Three-tier processing (fastest to slowest):
  1. DIRECT FILL  (~5ms)  — regex parses "fill X as Y", fuzzy matches X to field
  2. DIRECT EXPLAIN (~2ms) — status/explain queries answered from DOM data
  3. LLM (~800ms)          — only for truly ambiguous instructions

Field matching uses 5-layer fuzzy scoring so "category" matches:
  - field_id: "category", "enter_category", "_prkar_category"
  - label:    "Category", "(प्रकार) Category", "Item Category"
  - placeholder: "Enter Category"
"""
import logging
import re

from model_manager.mode_selector import OperationMode
from model_manager.ollama_client import OllamaUnavailableError, OllamaResponseError, generate
from reasoning_layer.prompt_engine import build_planning_prompt, build_retry_prompt
from reasoning_layer.sentence_parser import extract_slots, map_slots_to_fields, is_multi_slot_sentence
from reasoning_layer.schemas import ErrorAction, ExplainAction, LLMAction, ToolCall
from reasoning_layer.validator import parse_llm_output

logger = logging.getLogger(__name__)

MAX_RETRIES = 2

# Matches: fill/set/enter/... <field> as/to/= <value>
# Also matches without verb: "name as John", "email to a@b.com"
_FILL_RE = re.compile(
    r"^(?:(?:fill|set|enter|put|type|write|update|change|make)\s+)?(.+?)\s+(?:as|to|=|:)\s*(.+)$",
    re.IGNORECASE,
)

_EXPLAIN_TRIGGERS = {
    "explain", "what", "show", "describe", "tell", "which", "how", "list",
    "missing", "status", "check", "scan", "overview", "summary", "required",
    "fields", "incomplete", "filled", "unfilled", "analyse", "analyze",
}

# Words that are NOT field names — stripped from matching keywords
_NOISE = {
    "fill", "set", "enter", "put", "as", "to", "the", "a", "an", "with",
    "value", "field", "please", "and", "for", "in", "of", "is", "it", "me",
    "my", "this", "that", "on", "at", "be", "do", "go", "no", "ok", "yes",
    "can", "will", "i", "make", "update", "change", "type", "write",
}


# ── Field utilities ────────────────────────────────────────────────────────

def _all_fields(ctx: dict) -> list[dict]:
    return [f for s in ctx.get("sections", []) for f in s.get("fields", [])]


def _clean(text: str) -> str:
    """Strip Indic script, parentheticals, symbols → lowercase ASCII."""
    t = re.sub(r"\([^)]+\)", "", text)                      # (parenthetical)
    t = re.sub(r"[\u0900-\u0D7F]+", "", t)                  # Indic scripts
    t = re.sub(r"[*†‡§¶#@!?:\-_/\\|]", " ", t)
    return t.lower().strip()


def _tokenise(text: str) -> list[str]:
    """Split cleaned text into meaningful tokens, remove noise."""
    return [w for w in re.findall(r"[a-z0-9]+", _clean(text))
            if w not in _NOISE and len(w) > 1]


def _score_field(f: dict, query_tokens: list[str]) -> int:
    """
    Score a field against query tokens. Higher = better match.

    Key principle: ALL query tokens must match, or score is 0.
    This prevents "product name" from matching "product code" just because
    both share the "product" token.
    """
    if not query_tokens:
        return 0

    label = f.get("label", "")
    fid   = f.get("field_id", "")
    ph    = f.get("placeholder", "") or ""
    did   = f.get("dom_id", "") or ""

    clean_label = _clean(label)
    label_tokens = _tokenise(label)

    # Build all the text surfaces to match against (primary = label)
    surfaces = [
        clean_label,
        _clean(fid.replace("_", " ")),
        _clean(ph),
        _clean(did.replace("_", " ")),
        label.lower(),
    ]

    # ── Gate: every query token must appear somewhere ─────────────────────
    # If any query token is absent from ALL surfaces, score = 0 immediately.
    # This is the core fix: "product name" won't match "product code".
    for tok in query_tokens:
        found = any(
            tok in surface or any(tok == w for w in surface.split())
            for surface in surfaces if surface
        )
        if not found:
            return 0

    score = 0

    # ── Full phrase exact match (highest priority) ────────────────────────
    query_phrase = " ".join(query_tokens)
    if query_phrase == clean_label:
        score += 100    # exact phrase = label: "product name" == "product name"
    elif clean_label.startswith(query_phrase):
        score += 60     # label starts with full phrase
    elif query_phrase in clean_label:
        score += 40     # phrase is substring of label

    # ── Per-token scoring ─────────────────────────────────────────────────
    for tok in query_tokens:
        for surface in surfaces:
            if not surface:
                continue
            words = surface.split()
            if tok == surface.strip():    score += 20  # full surface exact
            elif tok in words:            score += 10  # exact word in surface
            elif surface.startswith(tok): score += 6   # surface starts with tok
            elif tok in surface:          score += 3   # substring

    # ── Precision bonus: fewer unmatched label words = better fit ────────
    # "product name" (2 words) should beat "product name extra" (3 words)
    # when query is "product name".
    unmatched = sum(1 for w in label_tokens if w not in query_tokens)
    score -= unmatched * 2   # small penalty per extra word in label

    # ── Exact label token count match bonus ──────────────────────────────
    if len(label_tokens) == len(query_tokens):
        score += 15   # same number of words = probably exact field name

    return max(score, 0)


def _find_best_field(ctx: dict, hint: str) -> tuple[str | None, str, int]:
    """
    Find the best matching non-readonly field for the hint string.
    Returns (field_id, label, score). Score=0 means no match found.
    """
    tokens = _tokenise(hint)
    if not tokens:
        return None, "", 0

    best_fid, best_lbl, best_score = None, "", 0

    for f in _all_fields(ctx):
        fid = f.get("field_id", "")
        if not fid:
            continue
        if f.get("readonly") or f.get("calculated"):
            continue
        s = _score_field(f, tokens)
        if s > best_score:
            best_score, best_fid, best_lbl = s, fid, f.get("label", "")

    return best_fid, best_lbl, best_score


# ── Direct fill ────────────────────────────────────────────────────────────

def _direct_fill(
    instruction: str,
    ctx: dict,
    calc: list[str],
    ro: list[str],
) -> LLMAction | None:
    """
    Parse 'fill X as Y' pattern and match X to a field without LLM.
    Accepts many phrasings:
      "fill category as chairs"
      "fill category to chairs"
      "set product name as kalu"
      "category to chairs"          ← no verb
      "name = John"
      "enter date as 2024-01-15"
    """
    m = _FILL_RE.match(instruction.strip())
    if not m:
        return None

    field_hint = m.group(1).strip()
    value      = m.group(2).strip()

    # Don't use the hint if it looks like a full sentence rather than a field name
    if len(field_hint.split()) > 6:
        return None

    # Strip trailing punctuation from value (STT adds periods at sentence end)
    value = value.rstrip(".,!?")

    fid, label, score = _find_best_field(ctx, field_hint)

    if not fid or score < 20:
        # Score too low = ambiguous, let LLM decide
        logger.debug("Direct fill: no confident match for '%s' (score=%d)", field_hint, score)
        return None

    if fid in calc or fid in ro:
        return ErrorAction(reason=f"'{fid}' is {'calculated' if fid in calc else 'readonly'} — cannot be set by LLM")

    logger.info("Direct fill ✓ '%s' → '%s' [score=%d] = '%s'", field_hint, fid, score, value)
    return ToolCall(field_id=fid, value=value, reason="direct match")


# ── Direct explain ─────────────────────────────────────────────────────────

def _direct_explain(instruction: str, ctx: dict) -> LLMAction | None:
    """Answer page-status queries instantly from DOM data."""
    words = set(instruction.lower().split())
    if not (words & _EXPLAIN_TRIGGERS):
        return None

    fields  = _all_fields(ctx)
    total   = len(fields)
    filled  = sum(1 for f in fields if f.get("value") and str(f["value"]).strip())
    missing = [
        (_clean(f.get("label", "")) or f["field_id"])
        for f in fields
        if f.get("required") and not (f.get("value") and str(f["value"]).strip())
    ]
    page    = ctx.get("page", {})
    title   = page.get("title", "this page")
    buttons = [b.get("label", "") for b in ctx.get("buttons", [])
               if not b.get("disabled") and b.get("label")]

    instr = instruction.lower()

    # "what fields are missing / required"
    if any(w in instr for w in ("missing", "required", "incomplete", "unfilled")):
        msg = f"Missing required fields: {', '.join(missing[:10])}" if missing \
              else "All required fields are filled."
        return ExplainAction(message=msg, related_fields=missing[:10])

    # "list all fields / show fields"
    if any(w in instr for w in ("list", "show", "fields", "field")):
        fillable = [
            (_clean(f.get("label", "")) or f["field_id"])
            for f in fields if not f.get("readonly") and not f.get("calculated")
        ]
        return ExplainAction(
            message=f"Fillable fields: {', '.join(fillable[:20])}",
            related_fields=[f["field_id"] for f in fields if not f.get("readonly")][:15],
        )

    # General explain / what / status
    msg = f"This is '{title}'. {total} fields, {filled} filled."
    if missing:
        msg += f" Still needed: {', '.join(missing[:5])}."
    if buttons:
        msg += f" Actions: {', '.join(buttons[:4])}."
    return ExplainAction(message=msg, related_fields=missing[:5])


# ── Controller ─────────────────────────────────────────────────────────────

class ReasoningController:
    def __init__(self, mode: OperationMode) -> None:
        self.mode = mode
        logger.info("ReasoningController ready — mode: %s", mode.value)

    def run(
        self,
        workflow_name: str,
        screen_context: dict,
        user_instruction: str,
        next_field: str | None,
        calculated_fields: list[str],
        required_fields:   list[str],
        missing_required:  list[str],
    ) -> LLMAction:

        readonly_fields = [
            f["field_id"] for f in _all_fields(screen_context)
            if f.get("readonly") and f.get("field_id")
        ]

        # ── Tier 1: direct fill — ~5ms ───────────────────────────────────────
        result = _direct_fill(user_instruction, screen_context, calculated_fields, readonly_fields)
        if result:
            return result

        # ── Tier 1.5: natural sentence multi-slot fill — ~2ms ──────────────────
        # "purchase for 50 chairs from ABC today"
        # Extracts multiple (field, value) slots without LLM.
        # Returns a list action so voice_controller queues all fills.
        if is_multi_slot_sentence(user_instruction):
            slots = extract_slots(user_instruction)
            if len(slots) >= 2:
                fills = map_slots_to_fields(slots, screen_context)
                # Filter out calculated/readonly fields
                fills = [
                    (fid, lbl, val) for fid, lbl, val in fills
                    if fid not in calculated_fields and fid not in readonly_fields
                ]
                if fills:
                    logger.info(
                        "Tier 1.5 multi-slot: %d fills from '%s'",
                        len(fills), user_instruction[:60],
                    )
                    # Return a special multi-fill action
                    # voice_controller recognises action="multi_fill"
                    return {
                        "action":  "multi_fill",
                        "fills":   [
                            {"action": "tool_call", "field_id": fid,
                             "label": lbl, "value": val, "reason": "sentence_parser"}
                            for fid, lbl, val in fills
                        ],
                        "reason": f"extracted {len(fills)} slots from sentence",
                    }

        # ── Tier 2: direct explain — ~2ms ────────────────────────────────────
        result = _direct_explain(user_instruction, screen_context)
        if result:
            return result

        # ── Tier 3: LLM — ~800ms ─────────────────────────────────────────────
        base_prompt = build_planning_prompt(
            workflow_name=workflow_name,
            screen_context=screen_context,
            user_instruction=user_instruction,
            next_field=next_field,
            calculated_fields=calculated_fields,
            readonly_fields=readonly_fields,
            required_fields=required_fields,
            missing_required=missing_required,
        )

        prompt       = base_prompt
        last_raw     = ""
        last_error   = ""

        for attempt in range(1, MAX_RETRIES + 1):
            logger.info("LLM attempt %d/%d — workflow: %s", attempt, MAX_RETRIES, workflow_name)
            try:
                raw = generate(prompt)
            except OllamaUnavailableError as exc:
                return ErrorAction(reason=f"Ollama unavailable: {exc}")
            except OllamaResponseError as exc:
                return ErrorAction(reason=f"Ollama error: {exc}")

            action = parse_llm_output(raw, calculated_fields, readonly_fields)

            if not isinstance(action, ErrorAction):
                logger.info("LLM OK attempt %d — %s", attempt, action.action)
                return action

            last_raw, last_error = raw, action.reason
            logger.warning("Attempt %d — %s", attempt, last_error)

            if attempt < MAX_RETRIES:
                prompt = build_retry_prompt(base_prompt, last_raw, last_error)

        logger.error("All %d attempts failed for '%s'", MAX_RETRIES, workflow_name)
        return ErrorAction(
            reason=f"LLM failed after {MAX_RETRIES} attempts. Last: {last_error}",
            raw_output=last_raw[:300],
            retry_count=MAX_RETRIES,
        )


# ── Singleton ──────────────────────────────────────────────────────────────
_instance: ReasoningController | None = None


def get_controller(mode: OperationMode) -> ReasoningController:
    global _instance
    if _instance is None:
        _instance = ReasoningController(mode=mode)
    return _instance