import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ── Categories ─────────────────────────────────────────────────────────────


class PluginCategory(str, Enum):
    WORKFLOW    = "workflow"
    SCREEN      = "screen"
    SECURITY    = "security"
    INTEGRATION = "integration"
    UTILITY     = "utility"


# ── Data models ────────────────────────────────────────────────────────────


@dataclass
class PluginMeta:
    name: str
    version: str
    category: PluginCategory
    enabled: bool = True
    config: dict = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)


@dataclass
class Plugin:
    meta: PluginMeta
    handler: Callable[..., Any]


# ── Registry ───────────────────────────────────────────────────────────────


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}

    def register(self, meta: PluginMeta, handler: Callable[..., Any]) -> None:
        if meta.name in self._plugins:
            raise ValueError(f"Plugin '{meta.name}' is already registered")

        missing = [dep for dep in meta.dependencies if dep not in self._plugins]
        if missing:
            raise ValueError(
                f"Plugin '{meta.name}' has unresolved dependencies: {missing}"
            )

        self._plugins[meta.name] = Plugin(meta=meta, handler=handler)
        logger.info(
            "Plugin registered: %s v%s [%s]",
            meta.name, meta.version, meta.category.value,
        )

    def get(self, name: str) -> Plugin:
        if name not in self._plugins:
            raise KeyError(f"Plugin '{name}' not found")
        plugin = self._plugins[name]
        if not plugin.meta.enabled:
            raise RuntimeError(f"Plugin '{name}' is registered but disabled")
        return plugin

    def enable(self, name: str) -> None:
        self._get_registered(name).meta.enabled = True
        logger.info("Plugin enabled: %s", name)

    def disable(self, name: str) -> None:
        self._get_registered(name).meta.enabled = False
        logger.info("Plugin disabled: %s", name)

    def execute(self, name: str, **kwargs: Any) -> Any:
        plugin = self.get(name)
        logger.debug("Executing plugin: %s", name)
        return plugin.handler(**kwargs)

    def list_plugins(self) -> list[dict[str, Any]]:
        return [
            {
                "name":         p.meta.name,
                "version":      p.meta.version,
                "category":     p.meta.category.value,
                "enabled":      p.meta.enabled,
                "dependencies": p.meta.dependencies,
            }
            for p in self._plugins.values()
        ]

    def list_by_category(self, category: PluginCategory) -> list[dict[str, Any]]:
        return [p for p in self.list_plugins() if p["category"] == category.value]

    def _get_registered(self, name: str) -> Plugin:
        if name not in self._plugins:
            raise KeyError(f"Plugin '{name}' not found")
        return self._plugins[name]


# ── Plugin handler implementations ────────────────────────────────────────


def _handle_screen_parse(raw_dom: dict) -> dict:
    from screen_context.parser import screen_context_parser
    result = screen_context_parser.parse(raw_dom)
    return result.model_dump()


def _handle_workflow_next(workflow_name: str, screen_context: dict) -> dict:
    """Advance a named workflow and return the next required field.
    Uses get_best() so it automatically falls back to FreeWorkflow
    when the page fields don't match the workflow's hardcoded IDs."""
    from workflow_core.registry import workflow_registry
    workflow = workflow_registry.get_best(workflow_name, screen_context)
    result = workflow.next_step(screen_context)
    return {
        "next_field":  result.next_field,
        "is_complete": result.is_complete,
        "errors":      result.errors,
    }


def _handle_permission_check(tool_name: str) -> dict:
    from security.permissions import tool_permission_guard
    result = tool_permission_guard.check(tool_name)
    return {
        "tool_name":   result.tool_name,
        "risk_level":  result.risk_level.value if result.risk_level else None,
        "outcome":     result.outcome.value,
        "reason":      result.reason,
    }


def _handle_audit_replay(session_id: str) -> dict:
    from logs.audit_logger import audit_logger
    entries = audit_logger.replay(session_id)
    return {"session_id": session_id, "entries": entries}


def _handle_mode_detect() -> dict:
    from core.engine import engine
    from model_manager.mode_selector import select_mode
    hw   = engine.hardware
    mode = select_mode(hw)
    return {
        "mode":          mode.value,
        "gpu_available": hw.gpu_available,
        "gpu_count":     hw.gpu_count,
    }


def _handle_field_validate(field_id: str, value: Any, workflow_name: str) -> dict:
    """Validate a single field value against workflow rules.
    Falls back to FreeWorkflow (no validation) for unknown field IDs."""
    from workflow_core.registry import workflow_registry
    # For field validation we need the actual workflow rules, not free mode.
    # Use plain get() here — validation is only meaningful against defined rules.
    try:
        workflow = workflow_registry.get(workflow_name)
    except KeyError:
        # Unknown workflow — treat as valid (free mode has no rules)
        return {"field_id": field_id, "value": value, "valid": True, "errors": []}

    mock_context = {
        "sections": [{
            "section_id": "validation_check",
            "title":      "Validation",
            "fields": [{"field_id": field_id, "value": value,
                        "required": field_id in workflow.required_fields,
                        "readonly": False, "calculated": False}],
        }]
    }
    errors = workflow.validate(mock_context)
    field_errors = [e for e in errors if field_id in e]
    return {
        "field_id": field_id,
        "value":    value,
        "valid":    len(field_errors) == 0,
        "errors":   field_errors,
    }


# ── Singleton and registrations ────────────────────────────────────────────


plugin_registry = PluginRegistry()

plugin_registry.register(
    PluginMeta(name="screen.parse", version="1.0.0", category=PluginCategory.SCREEN),
    _handle_screen_parse,
)

plugin_registry.register(
    PluginMeta(name="workflow.next", version="1.0.0", category=PluginCategory.WORKFLOW),
    _handle_workflow_next,
)

plugin_registry.register(
    PluginMeta(name="security.permission_check", version="1.0.0", category=PluginCategory.SECURITY),
    _handle_permission_check,
)

plugin_registry.register(
    PluginMeta(
        name="audit.replay", version="1.0.0", category=PluginCategory.UTILITY,
        dependencies=["security.permission_check"],
    ),
    _handle_audit_replay,
)

plugin_registry.register(
    PluginMeta(name="system.mode_detect", version="1.0.0", category=PluginCategory.UTILITY),
    _handle_mode_detect,
)

plugin_registry.register(
    PluginMeta(
        name="workflow.field_validate", version="1.0.0", category=PluginCategory.WORKFLOW,
        dependencies=["workflow.next"],
    ),
    _handle_field_validate,
)