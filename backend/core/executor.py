import hashlib
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

MAX_STEPS = 10
MAX_REFLECTION_RETRIES = 2


# ── Stop reasons ──────────────────────────────────────────────────────────


class StopReason(str, Enum):
    WORKFLOW_COMPLETE      = "workflow_complete"
    MAX_STEPS_REACHED      = "max_steps_reached"
    ERROR_ACTION_RETURNED  = "error_action_returned"
    PERMISSION_DENIED      = "permission_denied"
    CONFIRMATION_REQUIRED  = "confirmation_required"
    INFINITE_LOOP_DETECTED = "infinite_loop_detected"
    OLLAMA_UNAVAILABLE     = "ollama_unavailable"


# ── Step record ────────────────────────────────────────────────────────────


@dataclass
class StepRecord:
    step: int
    action_type: str
    field_id: str | None
    value: Any
    permission_outcome: str | None
    result: str
    reflection_retries: int


# ── Execution result ───────────────────────────────────────────────────────


@dataclass
class ExecutionResult:
    session_id: str
    workflow: str
    mode: str
    stop_reason: StopReason
    total_steps: int
    steps: list[StepRecord] = field(default_factory=list)
    final_action: dict | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_id":   self.session_id,
            "workflow":     self.workflow,
            "mode":         self.mode,
            "stop_reason":  self.stop_reason.value,
            "total_steps":  self.total_steps,
            "final_action": self.final_action,
            "errors":       self.errors,
            "steps": [
                {
                    "step":                s.step,
                    "action_type":         s.action_type,
                    "field_id":            s.field_id,
                    "value":               s.value,
                    "permission_outcome":  s.permission_outcome,
                    "result":              s.result,
                    "reflection_retries":  s.reflection_retries,
                }
                for s in self.steps
            ],
        }


# ── State hash for loop detection ─────────────────────────────────────────


def _state_hash(screen_context: dict, next_field: str | None) -> str:
    """
    Stable hash of the parts of state that must change between steps.
    If the same hash appears twice the controller detects a loop.
    """
    try:
        payload = json.dumps(
            {"next_field": next_field, "context": screen_context},
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        payload = str(screen_context) + str(next_field)
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


# ── Tool name resolution ───────────────────────────────────────────────────


def _resolve_tool_name(field_id: str | None, action_type: str) -> str:
    """Map a field_id / action combination to a registered tool name."""
    if not field_id:
        return "browser.click"
    fid = field_id.lower()
    if "submit" in fid or action_type == "submit":
        return "browser.submit"
    if "click" in fid or action_type == "click":
        return "browser.click"
    if "open" in fid or "app" in fid:
        return "system.open_app"
    return "browser.fill"


# ── Autonomous executor ────────────────────────────────────────────────────


class AutonomousExecutor:
    """
    Deterministic, step-bounded autonomous execution controller.

    Per-task rules enforced:
      - Maximum MAX_STEPS (10) action steps per task
      - Maximum MAX_REFLECTION_RETRIES (2) LLM retries per step
      - Halts immediately on ErrorAction from the reasoning layer
      - Halts immediately on permission BLOCKED or REQUIRES_CONFIRMATION
      - Halts immediately when the workflow reports completion
      - Loop detection via state hash — halts if the same state repeats

    This class has no I/O side effects. It calls the injected
    reasoning_controller and permission_guard, both of which are
    pure function objects from the rest of the platform.
    """

    def __init__(self, reasoning_controller, permission_guard, workflow, audit_logger=None):
        self._controller      = reasoning_controller
        self._permission      = permission_guard
        self._workflow        = workflow
        self._audit           = audit_logger

    def run(
        self,
        session_id: str,
        workflow_name: str,
        screen_context: dict,
        user_instruction: str,
        mode_value: str,
    ) -> ExecutionResult:

        result = ExecutionResult(
            session_id=session_id,
            workflow=workflow_name,
            mode=mode_value,
            stop_reason=StopReason.MAX_STEPS_REACHED,  # default; overwritten on clean exit
            total_steps=0,
        )

        seen_state_hashes: set[str] = set()

        logger.info(
            "AutonomousExecutor starting — session: %s, workflow: %s, max_steps: %d",
            session_id, workflow_name, MAX_STEPS,
        )

        for step_number in range(1, MAX_STEPS + 1):

            # ── 1. Determine workflow state ────────────────────────────────

            wf_result = self._workflow.next_step(screen_context)

            if wf_result.is_complete:
                logger.info("Step %d: workflow reports complete", step_number)
                result.stop_reason = StopReason.WORKFLOW_COMPLETE
                result.total_steps = step_number - 1
                return result

            # ── 2. Loop detection ──────────────────────────────────────────

            state_hash = _state_hash(screen_context, wf_result.next_field)
            if state_hash in seen_state_hashes:
                logger.error(
                    "Step %d: infinite loop detected — state hash %s repeated",
                    step_number, state_hash,
                )
                result.stop_reason = StopReason.INFINITE_LOOP_DETECTED
                result.total_steps = step_number - 1
                result.errors.append(
                    f"Infinite loop detected at step {step_number} "
                    f"(state hash {state_hash})"
                )
                return result
            seen_state_hashes.add(state_hash)

            # ── 3. Collect missing required fields ─────────────────────────

            missing_required = self._collect_missing(screen_context)

            # ── 4. Call reasoning layer ────────────────────────────────────

            logger.info(
                "Step %d: calling reasoning controller — next_field: %s",
                step_number, wf_result.next_field,
            )

            action = self._controller.run(
                workflow_name=workflow_name,
                screen_context=screen_context,
                user_instruction=user_instruction,
                next_field=wf_result.next_field,
                calculated_fields=self._workflow.calculated_fields,
                required_fields=self._workflow.required_fields,
                missing_required=missing_required,
            )

            action_dict = action.model_dump()
            action_type = action_dict.get("action", "unknown")

            # ── 5. Validate JSON structure ─────────────────────────────────

            if not self._is_valid_action_json(action_dict):
                logger.error("Step %d: action JSON failed structural validation", step_number)
                step_rec = StepRecord(
                    step=step_number,
                    action_type="error",
                    field_id=None,
                    value=None,
                    permission_outcome=None,
                    result="json_validation_failed",
                    reflection_retries=0,
                )
                result.steps.append(step_rec)
                result.errors.append(f"Step {step_number}: invalid action JSON structure")
                result.stop_reason = StopReason.ERROR_ACTION_RETURNED
                result.total_steps = step_number
                result.final_action = action_dict
                self._write_audit(
                    session_id, user_instruction, screen_context,
                    "", action_dict, None, "json_validation_failed",
                    mode_value, workflow_name,
                )
                return result

            # ── 6. Stop on ErrorAction ─────────────────────────────────────

            if action_type == "error":
                logger.error(
                    "Step %d: ErrorAction received — %s",
                    step_number, action_dict.get("reason"),
                )
                step_rec = StepRecord(
                    step=step_number,
                    action_type="error",
                    field_id=None,
                    value=None,
                    permission_outcome=None,
                    result="error_action",
                    reflection_retries=action_dict.get("retry_count", 0),
                )
                result.steps.append(step_rec)
                result.errors.append(action_dict.get("reason", "Unknown error"))
                result.stop_reason = StopReason.ERROR_ACTION_RETURNED
                result.total_steps = step_number
                result.final_action = action_dict
                self._write_audit(
                    session_id, user_instruction, screen_context,
                    action_dict.get("raw_output", ""), action_dict,
                    None, "error_action", mode_value, workflow_name,
                )
                return result

            # ── 7. Stop on non-tool actions (explain / confirmation) ───────

            if action_type in ("explain", "confirmation"):
                logger.info("Step %d: terminal action type '%s'", step_number, action_type)
                step_rec = StepRecord(
                    step=step_number,
                    action_type=action_type,
                    field_id=None,
                    value=None,
                    permission_outcome=None,
                    result=action_type,
                    reflection_retries=0,
                )
                result.steps.append(step_rec)
                result.stop_reason = (
                    StopReason.CONFIRMATION_REQUIRED
                    if action_type == "confirmation"
                    else StopReason.WORKFLOW_COMPLETE
                )
                result.total_steps = step_number
                result.final_action = action_dict
                self._write_audit(
                    session_id, user_instruction, screen_context,
                    "", action_dict, None, action_type, mode_value, workflow_name,
                )
                return result

            # ── 8. Permission check before execution ───────────────────────

            field_id  = action_dict.get("field_id")
            tool_name = _resolve_tool_name(field_id, action_type)
            perm      = self._permission.check(tool_name)
            perm_outcome = perm.outcome.value

            logger.info(
                "Step %d: permission check — tool: %s, outcome: %s",
                step_number, tool_name, perm_outcome,
            )

            if perm_outcome == "blocked":
                step_rec = StepRecord(
                    step=step_number,
                    action_type=action_type,
                    field_id=field_id,
                    value=action_dict.get("value"),
                    permission_outcome=perm_outcome,
                    result="permission_blocked",
                    reflection_retries=0,
                )
                result.steps.append(step_rec)
                result.errors.append(f"Step {step_number}: {perm.reason}")
                result.stop_reason = StopReason.PERMISSION_DENIED
                result.total_steps = step_number
                result.final_action = action_dict
                self._write_audit(
                    session_id, user_instruction, screen_context,
                    "", action_dict, tool_name, "permission_blocked",
                    mode_value, workflow_name,
                )
                return result

            if perm_outcome == "requires_confirmation":
                step_rec = StepRecord(
                    step=step_number,
                    action_type=action_type,
                    field_id=field_id,
                    value=action_dict.get("value"),
                    permission_outcome=perm_outcome,
                    result="confirmation_required",
                    reflection_retries=0,
                )
                result.steps.append(step_rec)
                result.stop_reason = StopReason.CONFIRMATION_REQUIRED
                result.total_steps = step_number
                result.final_action = action_dict
                self._write_audit(
                    session_id, user_instruction, screen_context,
                    "", action_dict, tool_name, "confirmation_required",
                    mode_value, workflow_name,
                )
                return result

            # ── 9. Apply action to screen context (deterministic state mutation)

            screen_context = self._apply_action(screen_context, field_id, action_dict.get("value"))

            # ── 10. Record step ────────────────────────────────────────────

            step_rec = StepRecord(
                step=step_number,
                action_type=action_type,
                field_id=field_id,
                value=action_dict.get("value"),
                permission_outcome=perm_outcome,
                result="executed",
                reflection_retries=action_dict.get("retry_count", 0),
            )
            result.steps.append(step_rec)

            self._write_audit(
                session_id, user_instruction, screen_context,
                "", action_dict, tool_name, "executed",
                mode_value, workflow_name,
            )

            logger.info(
                "Step %d: executed — field: %s, tool: %s",
                step_number, field_id, tool_name,
            )

        # Fell through all steps without completing
        logger.warning(
            "Session %s reached MAX_STEPS (%d) without completing workflow '%s'",
            session_id, MAX_STEPS, workflow_name,
        )
        result.stop_reason = StopReason.MAX_STEPS_REACHED
        result.total_steps = MAX_STEPS
        return result

    # ── Private helpers ────────────────────────────────────────────────────

    def _is_valid_action_json(self, action_dict: dict) -> bool:
        """Structural validation: action must be a dict with a non-empty 'action' key."""
        if not isinstance(action_dict, dict):
            return False
        action_val = action_dict.get("action")
        if not action_val or not isinstance(action_val, str):
            return False
        return True

    def _collect_missing(self, screen_context: dict) -> list[str]:
        missing: list[str] = []
        fields_by_id: dict[str, dict] = {}
        for section in screen_context.get("sections", []):
            for f in section.get("fields", []):
                fid = f.get("field_id")
                if fid:
                    fields_by_id[fid] = f
        for fid in self._workflow.required_fields:
            f = fields_by_id.get(fid, {})
            v = f.get("value")
            if v is None or str(v).strip() == "":
                missing.append(fid)
        return missing

    def _apply_action(
        self,
        screen_context: dict,
        field_id: str | None,
        value: Any,
    ) -> dict:
        """
        Return an updated copy of screen_context with the field value set.
        Does not mutate the original — creates a new dict at the sections level.
        Calculated and readonly fields are silently skipped.
        """
        if not field_id:
            return screen_context

        new_sections = []
        for section in screen_context.get("sections", []):
            new_fields = []
            for f in section.get("fields", []):
                if f.get("field_id") == field_id:
                    if f.get("readonly") or f.get("calculated"):
                        logger.warning(
                            "_apply_action: skipping readonly/calculated field '%s'",
                            field_id,
                        )
                        new_fields.append(f)
                    else:
                        new_fields.append({**f, "value": value})
                else:
                    new_fields.append(f)
            new_sections.append({**section, "fields": new_fields})

        return {**screen_context, "sections": new_sections}

    def _write_audit(
        self,
        session_id: str,
        user_input: str,
        screen_context: dict,
        llm_raw: str,
        validated_output: Any,
        tool_executed: str | None,
        result: str,
        mode: str,
        workflow: str,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.log(
                session_id=session_id,
                user_input=user_input,
                screen_context=screen_context,
                llm_raw_output=llm_raw,
                validated_output=validated_output,
                tool_executed=tool_executed,
                result=result,
                mode=mode,
                workflow=workflow,
            )
        except Exception as exc:
            logger.error("Audit write failed: %s", exc)