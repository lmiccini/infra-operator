# InstanceHA AI Layer
#
# Tool abstraction, safety/approval workflows, and (future) LLM integration.

from .tools import (
    ApprovalLevel,
    Tool,
    ToolRegistry,
    ToolResult,
    registry,
    tool,
)
from .safety import (
    ApprovalManager,
    AuditLogger,
    RateLimiter,
)

# Import tool modules to trigger @tool decorator registration
from . import read_tools    # noqa: F401
from . import write_tools   # noqa: F401
from . import diagnostic_tools  # noqa: F401

# Module-level observer reference, set by main.py _start_observer()
_observer = None
