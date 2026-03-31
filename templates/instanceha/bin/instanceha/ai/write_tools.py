"""Write tools that modify cluster state. Require approval."""

import logging
import sys

from .tools import ApprovalLevel, ToolResult, tool


@tool("fence_host", "Power off or on a host via configured fencing agent",
      approval_level=ApprovalLevel.CRITICAL, category="write",
      parameters={"host": "str", "action": "str (on|off)", "service": "InstanceHAService"})
def fence_host(host: str, action: str, service) -> ToolResult:
    """Fence a host using the configured fencing agent."""
    _pkg = sys.modules.get("instanceha")
    if not _pkg:
        return ToolResult(success=False, error="instanceha package not loaded")

    if action not in ("on", "off"):
        return ToolResult(success=False, error=f"Invalid action: {action}. Must be 'on' or 'off'")

    try:
        result = _pkg._host_fence(host, action, service)
        return ToolResult(
            success=bool(result),
            data={"host": host, "action": action},
            error=None if result else f"Fencing {action} failed for {host}",
        )
    except Exception as e:
        return ToolResult(success=False, error=str(e))


@tool("evacuate_host", "Evacuate all VMs from a failed host",
      approval_level=ApprovalLevel.CRITICAL, category="write",
      parameters={"connection": "OpenStackClient", "failed_service": "NovaService",
                   "service": "InstanceHAService", "target_host": "str (optional)"})
def evacuate_host(connection, failed_service, service, target_host=None) -> ToolResult:
    """Evacuate all VMs from a host."""
    _pkg = sys.modules.get("instanceha")
    if not _pkg:
        return ToolResult(success=False, error="instanceha package not loaded")

    try:
        if target_host:
            result = _pkg._host_evacuate(connection, failed_service, service, target_host)
        else:
            result = _pkg._host_evacuate(connection, failed_service, service)
        return ToolResult(
            success=bool(result),
            data={"host": failed_service.host, "target": target_host},
            error=None if result else f"Evacuation failed for {failed_service.host}",
        )
    except Exception as e:
        return ToolResult(success=False, error=str(e))


@tool("disable_host", "Force-down a compute service before evacuation",
      approval_level=ApprovalLevel.HIGH, category="write",
      parameters={"connection": "OpenStackClient", "nova_service": "NovaService",
                   "service": "InstanceHAService"})
def disable_host(connection, nova_service, service=None) -> ToolResult:
    """Disable (force-down) a compute service."""
    _pkg = sys.modules.get("instanceha")
    if not _pkg:
        return ToolResult(success=False, error="instanceha package not loaded")

    try:
        result = _pkg._host_disable(connection, nova_service, service)
        return ToolResult(
            success=bool(result),
            data={"host": nova_service.host},
            error=None if result else f"Disable failed for {nova_service.host}",
        )
    except Exception as e:
        return ToolResult(success=False, error=str(e))


@tool("enable_host", "Re-enable a compute service after evacuation",
      approval_level=ApprovalLevel.MEDIUM, category="write",
      parameters={"connection": "OpenStackClient", "nova_service": "NovaService",
                   "reenable": "bool", "service": "InstanceHAService (optional)"})
def enable_host(connection, nova_service, reenable: bool = False, service=None) -> ToolResult:
    """Enable a compute service."""
    _pkg = sys.modules.get("instanceha")
    if not _pkg:
        return ToolResult(success=False, error="instanceha package not loaded")

    try:
        result = _pkg._host_enable(connection, nova_service, reenable=reenable, service=service)
        return ToolResult(
            success=bool(result),
            data={"host": nova_service.host, "reenable": reenable},
            error=None if result else f"Enable failed for {nova_service.host}",
        )
    except Exception as e:
        return ToolResult(success=False, error=str(e))


@tool("evacuate_server", "Evacuate a single VM to another host",
      approval_level=ApprovalLevel.HIGH, category="write",
      parameters={"connection": "OpenStackClient", "server_id": "str",
                   "target_host": "str (optional)"})
def evacuate_server(connection, server_id: str, target_host=None) -> ToolResult:
    """Evacuate a single server."""
    _pkg = sys.modules.get("instanceha")
    if not _pkg:
        return ToolResult(success=False, error="instanceha package not loaded")

    try:
        result = _pkg._server_evacuate(connection, server_id, target_host=target_host)
        return ToolResult(
            success=result.accepted,
            data={"server_id": server_id, "target": target_host, "reason": result.reason},
            error=None if result.accepted else result.reason,
        )
    except Exception as e:
        return ToolResult(success=False, error=str(e))
