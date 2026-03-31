"""Tool registry and decorator for InstanceHA AI tools.

Tools are structured, auditable wrappers around existing InstanceHA functions.
Each tool declares its approval level, parameters, and whether it modifies state.
"""

import enum
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


class ApprovalLevel(enum.Enum):
    """Approval levels for tool execution."""
    NONE = "none"          # Read-only, no approval needed
    MEDIUM = "medium"      # Low-risk writes (e.g., enable host)
    HIGH = "high"          # Significant writes (e.g., disable host, evacuate VM)
    CRITICAL = "critical"  # Dangerous operations (e.g., fence, evacuate host)


@dataclass
class ToolResult:
    """Structured result from tool execution."""
    success: bool
    data: Any = None
    error: Optional[str] = None
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        result = {
            "success": self.success,
            "duration_seconds": round(self.duration_seconds, 3),
        }
        if self.data is not None:
            result["data"] = self.data
        if self.error is not None:
            result["error"] = self.error
        return result


@dataclass
class Tool:
    """Metadata and callable for a registered tool."""
    name: str
    description: str
    func: Callable
    approval_level: ApprovalLevel
    category: str
    parameters: Dict[str, str] = field(default_factory=dict)

    def to_schema(self) -> dict:
        """Return tool metadata as a schema dict (for LLM tool-calling)."""
        return {
            "name": self.name,
            "description": self.description,
            "approval_level": self.approval_level.value,
            "category": self.category,
            "parameters": self.parameters,
        }


class ToolRegistry:
    """Central registry of all available tools."""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool_obj: Tool) -> None:
        """Register a tool."""
        if tool_obj.name in self._tools:
            raise ValueError(f"Tool '{tool_obj.name}' already registered")
        self._tools[tool_obj.name] = tool_obj
        logging.debug("Registered tool: %s (%s)", tool_obj.name, tool_obj.approval_level.value)

    def get(self, name: str) -> Optional[Tool]:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self, category: Optional[str] = None) -> List[Tool]:
        """List all tools, optionally filtered by category."""
        tools = list(self._tools.values())
        if category:
            tools = [t for t in tools if t.category == category]
        return sorted(tools, key=lambda t: t.name)

    def get_schemas(self) -> List[dict]:
        """Return all tool schemas (for LLM system prompt)."""
        return [t.to_schema() for t in self.list_tools()]

    def execute(self, name: str, **kwargs) -> ToolResult:
        """Execute a tool by name. Approval checks are done externally."""
        tool_obj = self._tools.get(name)
        if not tool_obj:
            return ToolResult(success=False, error=f"Unknown tool: {name}")

        start = time.time()
        try:
            result = tool_obj.func(**kwargs)
            duration = time.time() - start

            if isinstance(result, ToolResult):
                result.duration_seconds = duration
                return result

            return ToolResult(success=True, data=result, duration_seconds=duration)

        except Exception as e:
            duration = time.time() - start
            logging.error("Tool %s failed: %s", name, e)
            return ToolResult(success=False, error=str(e), duration_seconds=duration)


# Module-level registry instance
registry = ToolRegistry()


def tool(name: str, description: str, approval_level: ApprovalLevel = ApprovalLevel.NONE,
         category: str = "general", parameters: Optional[Dict[str, str]] = None):
    """Decorator to register a function as a tool.

    Usage:
        @tool("get_config", "Get current configuration", category="read-only")
        def get_config(service):
            return service.config._config_map
    """
    def decorator(func: Callable) -> Callable:
        # Auto-extract parameter descriptions from function signature if not provided
        params = parameters or {}
        if not params:
            sig = inspect.signature(func)
            for param_name, param in sig.parameters.items():
                if param_name in ('self', 'cls'):
                    continue
                annotation = param.annotation
                if annotation != inspect.Parameter.empty:
                    params[param_name] = str(annotation.__name__) if hasattr(annotation, '__name__') else str(annotation)
                else:
                    params[param_name] = "any"

        tool_obj = Tool(
            name=name,
            description=description,
            func=func,
            approval_level=approval_level,
            category=category,
            parameters=params,
        )
        registry.register(tool_obj)
        func._tool = tool_obj
        return func

    return decorator
