"""MCP (Model Context Protocol) server for InstanceHA.

Exposes ToolRegistry tools as MCP tools and Observer alerts/events as MCP
resources.  Runs as a daemon thread on port 8081 via streamable-http.

This is an additional interface alongside the Unix socket chat server.
External MCP clients can connect to query cluster state and (with approval)
perform operations.

Requires: pip install "mcp>=1.25,<2"
"""

import json
import logging
import threading
from typing import Any, Dict, Optional

from .tools import ApprovalLevel, registry as tool_registry

_mcp_available = False
try:
    from mcp.server.fastmcp import FastMCP
    _mcp_available = True
except ImportError:
    pass

_INJECTED_PARAMS = {"connection", "service"}

MCP_PORT = 8081
MCP_HOST = "0.0.0.0"


def _filter_params(params: Dict[str, str]) -> Dict[str, str]:
    return {k: v for k, v in params.items() if k not in _INJECTED_PARAMS}


class InstanceHAMCPServer:
    """MCP server that wraps the InstanceHA tool registry.

    Read-only tools are exposed directly. Write tools go through the
    ApprovalManager. Observer alerts, cluster health, and events are
    exposed as MCP resources with ``instanceha://`` URI scheme.
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
        mcp = FastMCP(
            "InstanceHA",
            host=self._host,
            port=self._port,
            instructions=(
                "InstanceHA manages high availability for OpenStack compute "
                "hosts. Use the tools to query cluster state, diagnose "
                "failures, and (with approval) perform recovery actions."
            ),
        )

        for tool_obj in tool_registry.list_tools():
            self._register_tool(mcp, tool_obj)

        self._register_resources(mcp)

        return mcp

    def _register_tool(self, mcp: "FastMCP", tool_obj):
        is_write = tool_obj.approval_level != ApprovalLevel.NONE

        if is_write and not self._allow_writes:
            return

        description = tool_obj.description
        if is_write:
            description += f" [approval_level={tool_obj.approval_level.value}]"

        filtered_params = _filter_params(tool_obj.parameters or {})
        tool_name = tool_obj.name

        approval_manager = self._approval_manager
        server = self

        def _make_handler(name, has_params):
            if has_params:
                async def handler(**kwargs):
                    return server._execute(approval_manager, name, kwargs)
            else:
                async def handler():
                    return server._execute(approval_manager, name, {})
            handler.__name__ = name
            handler.__doc__ = description
            return handler

        mcp.tool()(_make_handler(tool_name, bool(filtered_params)))

    def _execute(self, approval_manager, tool_name: str, params: dict) -> str:
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
        inject = {}
        expected = tool_obj.parameters or {}
        if "connection" in expected:
            inject["connection"] = self._nova_connection
        if "service" in expected:
            inject["service"] = self._service
        return inject

    def _register_resources(self, mcp: "FastMCP"):
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
        try:
            from . import _observer
            return _observer
        except (ImportError, AttributeError):
            return None

    def start(self) -> Optional[threading.Thread]:
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
        try:
            self._mcp.run(transport="streamable-http")
        except Exception as e:
            logging.error("MCP server failed: %s", e)
            self._running = False

    def stop(self):
        self._running = False

    @property
    def is_available(self) -> bool:
        return _mcp_available

    def update_connection(self, nova_connection):
        self._nova_connection = nova_connection
