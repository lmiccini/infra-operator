"""MCP (Model Context Protocol) server for InstanceHA.

Exposes the existing ToolRegistry tools as MCP tools and the Observer
alerts/events as MCP resources.  Runs as a daemon thread inside the
InstanceHA pod, listening on port 8081 via streamable-http transport.

This is an *additional* interface alongside the Unix socket chat server.
External MCP clients (Claude Desktop, Cursor, etc.) can connect to query
cluster state and (with approval) perform operations.

Requires: pip install "mcp>=1.25,<2"
"""

import json
import logging
import threading
from typing import Any, Dict, List, Optional

from .tools import ApprovalLevel, ToolResult, registry as tool_registry

# MCP SDK imports are deferred to avoid hard failure when the
# package is not installed.  The server simply won't start.
_mcp_available = False
try:
    from mcp.server.fastmcp import FastMCP
    _mcp_available = True
except ImportError:
    pass


# Parameters that are injected at execution time, not exposed to MCP clients.
_INJECTED_PARAMS = {"connection", "service"}

MCP_PORT = 8081
MCP_HOST = "0.0.0.0"


def _filter_params(params: Dict[str, str]) -> Dict[str, str]:
    """Remove injected params from a tool's parameter dict."""
    return {k: v for k, v in params.items() if k not in _INJECTED_PARAMS}


def _build_tool_input_schema(tool_obj) -> dict:
    """Build a JSON Schema for an MCP tool from a ToolRegistry Tool."""
    filtered = _filter_params(tool_obj.parameters or {})
    properties = {}
    required = []

    for pname, ptype in filtered.items():
        prop = {"type": "string", "description": f"{pname} ({ptype})"}
        if ptype in ("int", "float"):
            prop["type"] = "number"
        elif ptype == "bool":
            prop["type"] = "boolean"
        properties[pname] = prop
        required.append(pname)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


class InstanceHAMCPServer:
    """MCP server that wraps the InstanceHA tool registry.

    Design:
    - Read-only tools are exposed directly as MCP tools.
    - Write tools are exposed but go through the ApprovalManager.
    - Observer alerts, cluster health, and events are exposed as MCP
      resources with ``instanceha://`` URI scheme.
    - Connection/service objects are injected at execution time (same
      pattern as the chat server).
    """

    def __init__(self, nova_connection, service,
                 approval_manager,
                 host: str = MCP_HOST,
                 port: int = MCP_PORT,
                 allow_writes: bool = False):
        self._nova_connection = nova_connection
        self._service = service
        self._approval_manager = approval_manager
        self._host = host
        self._port = port
        self._allow_writes = allow_writes
        self._mcp: Optional[Any] = None
        self._running = False

    def _create_mcp(self) -> "FastMCP":
        """Build the FastMCP instance and register tools + resources."""
        mcp = FastMCP(
            "InstanceHA",
            instructions=(
                "InstanceHA manages high availability for OpenStack compute "
                "hosts. Use the tools to query cluster state, diagnose "
                "failures, and (with approval) perform recovery actions."
            ),
        )

        # Register each ToolRegistry tool as an MCP tool
        for tool_obj in tool_registry.list_tools():
            self._register_tool(mcp, tool_obj)

        # Register MCP resources for observer data
        self._register_resources(mcp)

        return mcp

    def _register_tool(self, mcp: "FastMCP", tool_obj):
        """Register a single ToolRegistry tool as an MCP tool."""
        is_write = tool_obj.approval_level != ApprovalLevel.NONE

        # Skip write tools if writes are disabled
        if is_write and not self._allow_writes:
            return

        # Build a description that includes the approval level for writes
        description = tool_obj.description
        if is_write:
            description += f" [approval_level={tool_obj.approval_level.value}]"

        filtered_params = _filter_params(tool_obj.parameters or {})
        tool_name = tool_obj.name

        # Capture references for the closure
        approval_manager = self._approval_manager
        server = self

        # We register a dynamic handler function.  FastMCP will discover
        # its name and use it as the tool name.
        if not filtered_params:
            # No user-facing parameters -- e.g., get_compute_services
            async def _handler(_name=tool_name):
                return server._execute(approval_manager, _name, {})
            _handler.__name__ = tool_name
            _handler.__doc__ = description
            mcp.tool()(_handler)
        else:
            # Has user-facing parameters -- pass as kwargs dict
            async def _handler(_name=tool_name, **kwargs):
                return server._execute(approval_manager, _name, kwargs)
            _handler.__name__ = tool_name
            _handler.__doc__ = description
            mcp.tool()(_handler)

    def _execute(self, approval_manager, tool_name: str, params: dict) -> str:
        """Execute a tool through the approval manager, returning text."""
        tool_obj = tool_registry.get(tool_name)
        if tool_obj is None:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

        inject = self._build_inject_kwargs(tool_obj)

        result = approval_manager.execute_tool(
            tool_registry, tool_name, params,
            user="mcp-client",
            inject_kwargs=inject,
        )

        if result.error == "approval_required":
            return json.dumps({
                "status": "approval_required",
                "approval_id": result.data.get("approval_id", ""),
                "approval_level": result.data.get("approval_level", ""),
                "message": "This action requires operator approval. "
                           "Use the chat interface to approve.",
            })

        return json.dumps(result.to_dict(), default=str)

    def _build_inject_kwargs(self, tool_obj) -> dict:
        """Build kwargs to inject at execution time."""
        inject = {}
        expected = tool_obj.parameters or {}
        if "connection" in expected:
            inject["connection"] = self._nova_connection
        if "service" in expected:
            inject["service"] = self._service
        return inject

    def _register_resources(self, mcp: "FastMCP"):
        """Register MCP resources for observer data."""
        server = self

        @mcp.resource("instanceha://health")
        async def cluster_health() -> str:
            """Current cluster health assessment from the AI observer."""
            observer = server._get_observer()
            if observer is None:
                return json.dumps({"error": "Observer not running"})
            return json.dumps(observer.get_cluster_health(), default=str)

        @mcp.resource("instanceha://alerts")
        async def alerts() -> str:
            """Current unacknowledged alerts from the AI observer."""
            observer = server._get_observer()
            if observer is None:
                return json.dumps({"error": "Observer not running"})
            alert_list = observer.get_alerts(unacknowledged_only=True)
            return json.dumps([a.to_dict() for a in alert_list], default=str)

        @mcp.resource("instanceha://alerts/all")
        async def all_alerts() -> str:
            """All alerts (including acknowledged) from the AI observer."""
            observer = server._get_observer()
            if observer is None:
                return json.dumps({"error": "Observer not running"})
            alert_list = observer.get_alerts()
            return json.dumps([a.to_dict() for a in alert_list], default=str)

        @mcp.resource("instanceha://events")
        async def recent_events() -> str:
            """Recent events from the event bus (last 60 minutes)."""
            try:
                from .event_bus import get_event_bus
                bus = get_event_bus()
                events = bus.get_events(minutes=60)
                return json.dumps([e.to_dict() for e in events[-100:]],
                                  default=str)
            except Exception as e:
                return json.dumps({"error": str(e)})

        @mcp.resource("instanceha://host/{host}")
        async def host_analysis(host: str) -> str:
            """AI analysis for a specific compute host."""
            observer = server._get_observer()
            if observer is None:
                return json.dumps({"error": "Observer not running"})
            return json.dumps(observer.get_host_analysis(host), default=str)

    def _get_observer(self):
        """Get the global observer instance."""
        try:
            from . import _observer
            return _observer
        except (ImportError, AttributeError):
            return None

    def start(self) -> Optional[threading.Thread]:
        """Start the MCP server in a daemon thread."""
        if not _mcp_available:
            logging.info("MCP SDK not installed, MCP server disabled")
            return None

        self._mcp = self._create_mcp()
        self._running = True

        thread = threading.Thread(
            target=self._serve,
            name="mcp-server",
            daemon=True,
        )
        thread.start()
        logging.info("MCP server started on %s:%d", self._host, self._port)
        return thread

    def _serve(self):
        """Run the MCP server (blocking)."""
        try:
            self._mcp.run(
                transport="streamable-http",
                host=self._host,
                port=self._port,
            )
        except Exception as e:
            logging.error("MCP server failed: %s", e)
            self._running = False

    def stop(self):
        """Signal the MCP server to stop."""
        self._running = False

    @property
    def is_available(self) -> bool:
        """Whether the MCP SDK is installed and server can start."""
        return _mcp_available

    def update_connection(self, nova_connection):
        """Update the Nova connection reference."""
        self._nova_connection = nova_connection
