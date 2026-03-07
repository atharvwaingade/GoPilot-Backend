import json
import logging
import re
import threading
from typing import Any

from pydantic import ValidationError

from reasoning_layer.schemas import (
    ActionType, ConfirmationAction, ErrorAction, ExplainAction, LLMAction, ToolCall,
)

logger = logging.getLogger(__name__)

# Module-level queue for multi-fill batches from LLM array responses.
# voice_controller drains this after processing the first action.
# Protected by a lock so concurrent requests don't corrupt each other's batches.
_llm_batch_lock:  threading.Lock = threading.Lock()
_llm_batch_queue: list[dict] = []


def pop_llm_batch() -> list[dict]:
    """Drain and return any pending batch fills from the last LLM array response."""
    with _llm_batch_lock:
        batch = list(_llm_batch_queue)
        _llm_batch_queue.clear()
    return batch

_JSON_GREEDY_RE    = re.compile(r"\{.*\}",  re.DOTALL)
_JSON_NONGREEDY_RE = re.compile(r"\{.*?\}", re.DOTALL)

_ACTION_MODEL_MAP = {
    ActionType.TOOL_CALL:    ToolCall,
    ActionType.EXPLAIN:      ExplainAction,
    ActionType.CONFIRMATION: ConfirmationAction,
    ActionType.ERROR:        ErrorAction,
}

_STRIP_KEYS   = {"thought","thoughts","reasoning","explanation","step","steps","note","notes"}
_VALID_ACTIONS = {a.value for a in ActionType}

# Keys the model sometimes uses instead of "value"
_VALUE_ALIASES = {"val", "input", "content", "data", "text", "entry", "new_value", "set_value"}

# Keys the model sometimes uses instead of "field_id"
_FIELD_ID_ALIASES = {"field", "field_name", "name", "target", "key", "id", "form_field"}


def _strip_to_json(raw: str) -> str | None:
    cleaned = re.sub(r"```(?:json)?|```", "", raw, flags=re.IGNORECASE).strip()

    m = _JSON_GREEDY_RE.search(cleaned)
    if m:
        try:
            json.loads(m.group(0))
            return m.group(0)
        except json.JSONDecodeError:
            pass

    for m in _JSON_NONGREEDY_RE.finditer(cleaned):
        try:
            json.loads(m.group(0))
            return m.group(0)
        except json.JSONDecodeError:
            continue

    opens = cleaned.count("{") - cleaned.count("}")
    if opens > 0:
        repaired = cleaned + ("}" * opens)
        try:
            json.loads(repaired)
            return repaired
        except json.JSONDecodeError:
            pass

    return None


def _normalise_action(data: dict) -> dict:
    """
    Remap hallucinated action names and wrong key names to the correct schema.

    Handles the specific qwen2.5:3b pattern:
      {"action":"set_category","instruction":"fill category as chairs","category":"chairs"}
    Which should be:
      {"action":"tool_call","field_id":"<category_field_id>","value":"chairs"}
    """
    action = str(data.get("action", ""))

    # ── 1. Remap action verb+field to tool_call ────────────────────────────
    field_verb = re.match(
        r"^(?:set|fill|enter|update|write|put)_?(.+)$", action, re.IGNORECASE
    )
    if field_verb and action not in _VALID_ACTIONS:
        inferred_fid = field_verb.group(1)
        logger.warning("Remapping action '%s' → tool_call (field hint: '%s')", action, inferred_fid)
        data = dict(data)
        data["action"] = "tool_call"
        # Only infer field_id from action name if not already present
        if not data.get("field_id"):
            data["field_id"] = inferred_fid

    # Synonym actions
    if data.get("action", "").lower() in {"toolcall","tool-call","call","execute","run","do"}:
        data = dict(data)
        data["action"] = "tool_call"

    # ── 2. Infer action from keys if missing ──────────────────────────────
    if not data.get("action"):
        if data.get("field_id") or any(k in data for k in _FIELD_ID_ALIASES):
            data = dict(data)
            data["action"] = "tool_call"
        elif data.get("message"):
            data = dict(data)
            data["action"] = "explain"

    # ── 3. Remap value aliases → "value" ──────────────────────────────────
    if data.get("action") == "tool_call" and "value" not in data:
        for alias in _VALUE_ALIASES:
            if alias in data:
                data = dict(data)
                data["value"] = data.pop(alias)
                logger.warning("Remapped key '%s' → 'value'", alias)
                break

    # ── 4. Remap field_id aliases → "field_id" ────────────────────────────
    if data.get("action") == "tool_call" and "field_id" not in data:
        for alias in _FIELD_ID_ALIASES:
            if alias in data:
                data = dict(data)
                data["field_id"] = data.pop(alias)
                logger.warning("Remapped key '%s' → 'field_id'", alias)
                break

    # ── 5. Handle the specific qwen pattern: extra keys ARE the field+value ─
    # Pattern: {"action":"tool_call","field_id":"category","instruction":"...","category":"chairs"}
    # "category":"chairs" is the value the model meant to set
    if data.get("action") == "tool_call":
        fid = data.get("field_id", "")
        # If there's a key matching the field_id hint that isn't a standard key, use it as value
        standard_keys = {"action","field_id","value","reason","instruction",
                         "workflow_name","message","fields_to_confirm","related_fields"}
        if fid and "value" not in data:
            # Look for a key that matches the field_id name
            if fid in data:
                data = dict(data)
                data["value"] = data[fid]
                logger.warning("Extracted value from key '%s' = '%s'", fid, data["value"])
            else:
                # Take the first non-standard key's value
                extra = {k: v for k, v in data.items() if k not in standard_keys}
                if extra:
                    k, v = next(iter(extra.items()))
                    data = dict(data)
                    data["value"] = v
                    logger.warning("Inferred value from extra key '%s' = '%s'", k, v)

    return data


def _sanitise(data: dict, action_type: ActionType) -> dict:
    # Strip known extra keys that cause extra=forbid failures
    standard = {"action","field_id","value","reason","message","related_fields",
                "fields_to_confirm","workflow_name","raw_output","retry_count"}
    cleaned = {k: v for k, v in data.items()
               if k in standard or k not in _STRIP_KEYS}
    # Keep only schema-valid keys per action type
    if action_type == ActionType.TOOL_CALL:
        cleaned = {k: v for k, v in cleaned.items()
                   if k in {"action","field_id","value","reason"}}
        if not cleaned.get("reason"):
            cleaned["reason"] = ""
        val = cleaned.get("value")
        if val in ("None","null","undefined"):
            cleaned["value"] = None

    elif action_type == ActionType.EXPLAIN:
        cleaned = {k: v for k, v in cleaned.items()
                   if k in {"action","message","related_fields"}}
        if not cleaned.get("message"):
            cleaned["message"] = "No explanation provided."
        if "related_fields" not in cleaned:
            cleaned["related_fields"] = []

    elif action_type == ActionType.CONFIRMATION:
        cleaned = {k: v for k, v in cleaned.items()
                   if k in {"action","message","fields_to_confirm","workflow_name"}}
        if not cleaned.get("message"):
            cleaned["message"] = "Please confirm this action."
        if not cleaned.get("workflow_name"):
            cleaned["workflow_name"] = "unknown"
        if "fields_to_confirm" not in cleaned:
            cleaned["fields_to_confirm"] = []

    elif action_type == ActionType.ERROR:
        cleaned = {k: v for k, v in cleaned.items()
                   if k in {"action","reason","raw_output","retry_count"}}

    return cleaned


def parse_llm_output(raw: str, calculated_fields: list[str], readonly_fields: list[str]) -> LLMAction:
    # ── Pre-validation: reject obviously hallucinated output ─────────────────
    # When the model echoes back the prompt (e.g. outputs {"field_id":"fields","value":"MAP:..."})
    # the value contains JSON-like content.  Reject immediately.
    _PROMPT_ECHO_RE = re.compile(
        r"""(?x)
        ^\s*(?:MAP:|CMD:|OUT:|FIELDS:|TASK:|JSON:)   # prompt prefix echoed as value
        | \{["\s]*action["\s]*:                       # raw action JSON inside value
        | [{}]{2,}                                    # }{} artifact
        """, re.IGNORECASE
    )
    _RESERVED_FIELD_IDS = {"fields", "field_map", "map", "fieldmap", "fields_map",
                           "task", "json", "out", "cmd", "instruction"}

    # ── Handle JSON array (multi-fill response) ────────────────────────────
    # The upgraded system prompt can return an array of tool_calls.
    # We return the FIRST one here; the rest are handled by the multi-fill
    # queue in voice_controller. The raw array is stored on the action.
    cleaned_raw = re.sub(r"```(?:json)?|```", "", raw, flags=re.IGNORECASE).strip()
    if cleaned_raw.startswith("["):
        try:
            arr = json.loads(cleaned_raw)
            if isinstance(arr, list) and arr:
                # Parse and validate each element
                valid_actions = []
                for item in arr:
                    if not isinstance(item, dict):
                        continue
                    item = _normalise_action(item)
                    action_raw = item.get("action", "")
                    try:
                        at = ActionType(action_raw)
                    except ValueError:
                        continue
                    item = _sanitise(item, at)
                    try:
                        act = _ACTION_MODEL_MAP[at].model_validate(item)
                        if isinstance(act, ToolCall):
                            if act.field_id in calculated_fields or act.field_id in readonly_fields:
                                continue
                        valid_actions.append(act)
                    except Exception as _ve:
                        logger.debug("Array element validation failed: %s", _ve)
                        continue
                if valid_actions:
                    logger.info("LLM returned %d-fill array", len(valid_actions))
                    # Store batch in module-level queue — voice_controller drains it
                    if len(valid_actions) > 1:
                        with _llm_batch_lock:
                            _llm_batch_queue.clear()
                            _llm_batch_queue.extend(
                                a.model_dump() for a in valid_actions[1:]
                            )
                    return valid_actions[0]
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("Array parse failed: %s", exc)
        # Fall through to single-object parse

    json_str = _strip_to_json(raw)
    if not json_str:
        logger.warning("No JSON in output: %s", raw[:200])
        return ErrorAction(reason="LLM returned no JSON object", raw_output=raw[:500])

    try:
        data: dict[str, Any] = json.loads(json_str)
    except json.JSONDecodeError as exc:
        return ErrorAction(reason=f"Invalid JSON: {exc}", raw_output=raw[:500])

    if not isinstance(data, dict):
        return ErrorAction(reason="Output must be a JSON object", raw_output=raw[:500])

    # Normalise hallucinated keys/actions
    data = _normalise_action(data)

    # Resolve action type
    action_raw = data.get("action", "")
    try:
        action_type = ActionType(action_raw)
    except ValueError:
        return ErrorAction(
            reason=f"Unknown action '{action_raw}'. Must be one of: {sorted(_VALID_ACTIONS)}",
            raw_output=raw[:500],
        )

    # Sanitise to only valid keys for this action type
    data = _sanitise(data, action_type)

    try:
        action = _ACTION_MODEL_MAP[action_type].model_validate(data)
    except ValidationError as exc:
        logger.warning("Pydantic validation failed: %s", exc)
        return ErrorAction(
            reason=f"Schema validation failed: {exc.errors()[0]['msg']}",
            raw_output=raw[:500],
        )

    if isinstance(action, ToolCall):
        if action.field_id in calculated_fields:
            return ErrorAction(reason=f"'{action.field_id}' is calculated", raw_output=raw[:500])
        if action.field_id in readonly_fields:
            return ErrorAction(reason=f"'{action.field_id}' is readonly", raw_output=raw[:500])
        # Reject prompt-echo hallucinations
        if action.field_id.lower() in _RESERVED_FIELD_IDS:
            return ErrorAction(
                reason=f"Hallucinated field_id '{action.field_id}' (reserved name)",
                raw_output=raw[:500],
            )
        if action.value and _PROMPT_ECHO_RE.search(str(action.value)):
            return ErrorAction(
                reason=f"Hallucinated value — prompt echo: {str(action.value)[:80]}",
                raw_output=raw[:500],
            )

    logger.debug("Validated: %s field_id=%s", action_type.value,
                 getattr(action, 'field_id', '-'))
    return action