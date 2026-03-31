"""Safety, approval, rate limiting, and audit logging for InstanceHA AI tools.

All write operations go through approval gates and are logged to an audit trail.
"""

import json
import logging
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from .tools import ApprovalLevel, ToolResult


AUDIT_LOG_PATH = "/var/log/instanceha/ai-audit.log"


@dataclass
class RateLimit:
    """Rate limit configuration for an operation type."""
    max_calls: int
    window_seconds: float


class RateLimiter:
    """Token-bucket rate limiter for tool operations."""

    # Default rate limits per approval level
    DEFAULT_LIMITS = {
        ApprovalLevel.CRITICAL: RateLimit(max_calls=3, window_seconds=60.0),
        ApprovalLevel.HIGH: RateLimit(max_calls=5, window_seconds=60.0),
        ApprovalLevel.MEDIUM: RateLimit(max_calls=10, window_seconds=60.0),
    }

    def __init__(self, limits: Optional[Dict[ApprovalLevel, RateLimit]] = None):
        self._limits = limits or self.DEFAULT_LIMITS
        self._calls: Dict[ApprovalLevel, List[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def check(self, level: ApprovalLevel) -> bool:
        """Check if an operation at the given level is allowed."""
        if level == ApprovalLevel.NONE:
            return True

        limit = self._limits.get(level)
        if not limit:
            return True

        now = time.time()
        with self._lock:
            # Prune old entries
            cutoff = now - limit.window_seconds
            self._calls[level] = [t for t in self._calls[level] if t > cutoff]

            if len(self._calls[level]) >= limit.max_calls:
                return False

            self._calls[level].append(now)
            return True

    def time_until_available(self, level: ApprovalLevel) -> float:
        """Return seconds until the next operation at this level is allowed."""
        if level == ApprovalLevel.NONE:
            return 0.0

        limit = self._limits.get(level)
        if not limit:
            return 0.0

        now = time.time()
        with self._lock:
            cutoff = now - limit.window_seconds
            calls = [t for t in self._calls[level] if t > cutoff]

            if len(calls) < limit.max_calls:
                return 0.0

            oldest = min(calls)
            return max(0.0, (oldest + limit.window_seconds) - now)


class AuditLogger:
    """Append-only audit log for all AI tool invocations."""

    def __init__(self, log_path: str = AUDIT_LOG_PATH):
        self._log_path = log_path
        self._lock = threading.Lock()
        self._ensure_directory()

    def _ensure_directory(self):
        """Create log directory if it doesn't exist."""
        log_dir = os.path.dirname(self._log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

    def log(self, tool_name: str, parameters: dict, result: ToolResult,
            approval_level: str, approved: bool, user: str = "system",
            reasoning: str = "") -> None:
        """Write an audit entry."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "tool": tool_name,
            "parameters": _sanitize_params(parameters),
            "approval_level": approval_level,
            "approved": approved,
            "user": user,
            "reasoning": reasoning,
            "result": {
                "success": result.success,
                "error": result.error,
                "duration_seconds": result.duration_seconds,
            },
        }

        with self._lock:
            try:
                with open(self._log_path, "a") as f:
                    f.write(json.dumps(entry) + "\n")
            except OSError as e:
                logging.warning("Failed to write audit log: %s", e)

    def get_recent(self, count: int = 50) -> List[dict]:
        """Read the most recent audit entries."""
        try:
            with open(self._log_path, "r") as f:
                lines = f.readlines()
            entries = []
            for line in lines[-count:]:
                try:
                    entries.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue
            return entries
        except FileNotFoundError:
            return []
        except OSError as e:
            logging.warning("Failed to read audit log: %s", e)
            return []


def _sanitize_params(params: dict) -> dict:
    """Remove sensitive values from parameters before logging."""
    sensitive_keys = {'password', 'passwd', 'token', 'secret', 'credential'}
    sanitized = {}
    for k, v in params.items():
        if any(s in k.lower() for s in sensitive_keys):
            sanitized[k] = "***"
        elif isinstance(v, dict):
            sanitized[k] = _sanitize_params(v)
        else:
            sanitized[k] = v
    return sanitized


class ApprovalManager:
    """Manages approval workflow for write operations.

    In interactive mode (chat), prompts the user for confirmation.
    In auto-approve mode, approves based on configured level threshold.
    """

    def __init__(self, auto_approve_level: ApprovalLevel = ApprovalLevel.NONE,
                 rate_limiter: Optional[RateLimiter] = None,
                 audit_logger: Optional[AuditLogger] = None,
                 dry_run: bool = True):
        self.auto_approve_level = auto_approve_level
        self.rate_limiter = rate_limiter or RateLimiter()
        self.audit_logger = audit_logger or AuditLogger()
        self.dry_run = dry_run
        self._pending_approvals: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def _level_value(self, level: ApprovalLevel) -> int:
        """Numeric ordering for approval levels."""
        order = {
            ApprovalLevel.NONE: 0,
            ApprovalLevel.MEDIUM: 1,
            ApprovalLevel.HIGH: 2,
            ApprovalLevel.CRITICAL: 3,
        }
        return order.get(level, 0)

    def needs_approval(self, level: ApprovalLevel) -> bool:
        """Check if an operation at this level requires interactive approval."""
        if level == ApprovalLevel.NONE:
            return False
        # CRITICAL always requires approval regardless of auto_approve setting
        if level == ApprovalLevel.CRITICAL:
            return True
        return self._level_value(level) > self._level_value(self.auto_approve_level)

    def check_rate_limit(self, level: ApprovalLevel) -> bool:
        """Check if rate limit allows this operation."""
        return self.rate_limiter.check(level)

    def request_approval(self, tool_name: str, parameters: dict,
                         approval_level: ApprovalLevel) -> str:
        """Create a pending approval request. Returns an approval ID."""
        import uuid
        approval_id = str(uuid.uuid4())[:8]

        with self._lock:
            self._pending_approvals[approval_id] = {
                "tool_name": tool_name,
                "parameters": parameters,
                "approval_level": approval_level,
                "timestamp": time.time(),
            }

        return approval_id

    def approve(self, approval_id: str) -> Optional[dict]:
        """Approve a pending request. Returns the request details or None."""
        with self._lock:
            return self._pending_approvals.pop(approval_id, None)

    def deny(self, approval_id: str) -> Optional[dict]:
        """Deny a pending request. Returns the request details or None."""
        with self._lock:
            request = self._pending_approvals.pop(approval_id, None)
            if request:
                # Log the denial
                self.audit_logger.log(
                    tool_name=request["tool_name"],
                    parameters=request["parameters"],
                    result=ToolResult(success=False, error="Denied by user"),
                    approval_level=request["approval_level"].value,
                    approved=False,
                )
            return request

    def get_pending(self) -> Dict[str, dict]:
        """Get all pending approval requests."""
        with self._lock:
            return dict(self._pending_approvals)

    def execute_tool(self, tool_registry, tool_name: str, parameters: dict,
                     user: str = "system", reasoning: str = "",
                     force_approve: bool = False,
                     inject_kwargs: Optional[Dict] = None) -> ToolResult:
        """Execute a tool with full safety checks.

        Returns ToolResult. If approval is needed and not force_approved,
        returns a result with error="approval_required" and the approval_id
        in data.
        """
        from .tools import registry as default_registry
        reg = tool_registry or default_registry

        tool_obj = reg.get(tool_name)
        if not tool_obj:
            return ToolResult(success=False, error=f"Unknown tool: {tool_name}")

        level = tool_obj.approval_level

        # Rate limit check
        if not self.check_rate_limit(level):
            wait_time = self.rate_limiter.time_until_available(level)
            result = ToolResult(
                success=False,
                error=f"Rate limited. Try again in {wait_time:.0f}s",
                data={"wait_seconds": wait_time},
            )
            self.audit_logger.log(tool_name, parameters, result,
                                  level.value, False, user, reasoning)
            return result

        # Approval check
        if self.needs_approval(level) and not force_approve:
            approval_id = self.request_approval(tool_name, parameters, level)
            return ToolResult(
                success=False,
                error="approval_required",
                data={"approval_id": approval_id, "approval_level": level.value},
            )

        # Dry-run check
        if self.dry_run and level != ApprovalLevel.NONE:
            result = ToolResult(
                success=True,
                data={"dry_run": True, "would_execute": tool_name, "parameters": parameters},
            )
            self.audit_logger.log(tool_name, parameters, result,
                                  level.value, True, user, f"[DRY-RUN] {reasoning}")
            return result

        # Execute -- merge any injected kwargs (connection, service) at call time
        exec_params = dict(parameters)
        if inject_kwargs:
            exec_params.update(inject_kwargs)
        result = reg.execute(tool_name, **exec_params)
        self.audit_logger.log(tool_name, parameters, result,
                              level.value, True, user, reasoning)
        return result
