import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PermissionOutcome(str, Enum):
    AUTO_EXECUTE = "auto_execute"
    LOG_AND_EXECUTE = "log_and_execute"
    REQUIRES_CONFIRMATION = "requires_confirmation"
    BLOCKED = "blocked"


# ── Tool registry ──────────────────────────────────────────────────────────
# Maps tool_name → RiskLevel.
# Any tool NOT in this registry is unconditionally blocked.

TOOL_REGISTRY: dict[str, RiskLevel] = {
    "browser.fill":    RiskLevel.MEDIUM,
    "browser.submit":  RiskLevel.HIGH,
    "browser.click":   RiskLevel.MEDIUM,
    "system.open_app": RiskLevel.HIGH,
}

# ── Risk level → execution policy ─────────────────────────────────────────

_POLICY: dict[RiskLevel, PermissionOutcome] = {
    RiskLevel.LOW:    PermissionOutcome.AUTO_EXECUTE,
    RiskLevel.MEDIUM: PermissionOutcome.LOG_AND_EXECUTE,
    RiskLevel.HIGH:   PermissionOutcome.REQUIRES_CONFIRMATION,
}


@dataclass(frozen=True)
class PermissionResult:
    tool_name: str
    risk_level: RiskLevel | None
    outcome: PermissionOutcome
    reason: str


class ToolPermissionGuard:
    """
    Evaluates whether a named tool may be executed and under what conditions.

    Rules:
    - Tool name must be a non-empty string — undefined names are blocked.
    - Tool must exist in TOOL_REGISTRY — unregistered tools are blocked.
    - LOW risk    → AUTO_EXECUTE   (no logging required)
    - MEDIUM risk → LOG_AND_EXECUTE (action is logged, then allowed)
    - HIGH risk   → REQUIRES_CONFIRMATION (caller must obtain user approval)
    """

    def check(self, tool_name: str) -> PermissionResult:
        # Guard: undefined or blank tool name
        if not tool_name or not tool_name.strip():
            logger.error("Permission denied — tool name is undefined or empty")
            return PermissionResult(
                tool_name=tool_name,
                risk_level=None,
                outcome=PermissionOutcome.BLOCKED,
                reason="Tool name is undefined or empty",
            )

        # Guard: tool not in registry
        if tool_name not in TOOL_REGISTRY:
            logger.error(
                "Permission denied — tool '%s' is not in the registry", tool_name
            )
            return PermissionResult(
                tool_name=tool_name,
                risk_level=None,
                outcome=PermissionOutcome.BLOCKED,
                reason=f"Tool '{tool_name}' is not registered. "
                       f"Registered tools: {sorted(TOOL_REGISTRY.keys())}",
            )

        risk_level = TOOL_REGISTRY[tool_name]
        outcome = _POLICY[risk_level]

        if outcome == PermissionOutcome.AUTO_EXECUTE:
            logger.debug("Tool '%s' [%s] → auto execute", tool_name, risk_level.value)

        elif outcome == PermissionOutcome.LOG_AND_EXECUTE:
            logger.info(
                "Tool '%s' [%s] → logged and allowed", tool_name, risk_level.value
            )

        elif outcome == PermissionOutcome.REQUIRES_CONFIRMATION:
            logger.warning(
                "Tool '%s' [%s] → requires user confirmation before execution",
                tool_name,
                risk_level.value,
            )

        return PermissionResult(
            tool_name=tool_name,
            risk_level=risk_level,
            outcome=outcome,
            reason=_outcome_reason(outcome, tool_name, risk_level),
        )

    def is_executable(self, tool_name: str) -> bool:
        """
        Returns True only if the tool may proceed without additional gating.
        HIGH-risk and unregistered tools return False.
        """
        result = self.check(tool_name)
        return result.outcome in (
            PermissionOutcome.AUTO_EXECUTE,
            PermissionOutcome.LOG_AND_EXECUTE,
        )


def _outcome_reason(
    outcome: PermissionOutcome, tool_name: str, risk_level: RiskLevel
) -> str:
    if outcome == PermissionOutcome.AUTO_EXECUTE:
        return f"Tool '{tool_name}' is low-risk and will execute automatically"
    if outcome == PermissionOutcome.LOG_AND_EXECUTE:
        return f"Tool '{tool_name}' is medium-risk; action has been logged"
    if outcome == PermissionOutcome.REQUIRES_CONFIRMATION:
        return (
            f"Tool '{tool_name}' is high-risk ({risk_level.value}); "
            f"user confirmation required before execution"
        )
    return f"Tool '{tool_name}' is blocked"


# Singleton used across the application
tool_permission_guard = ToolPermissionGuard()