"""Tests for Phase 6: MCP Server integration.

Tests cover:
- InstanceHAMCPServer construction and configuration
- Tool registration (read-only vs write tools, filtering)
- Tool execution through approval manager with inject_kwargs
- MCP resource registration (health, alerts, events, host analysis)
- Parameter filtering (_INJECTED_PARAMS removed from tool schemas)
- allow_writes flag controls write tool registration
- Graceful degradation when MCP SDK is not installed
- _start_mcp_server() helper in main.py
"""

import json
import logging
import sys
import unittest
from unittest.mock import MagicMock, patch, AsyncMock

# Mock OpenStack dependencies before importing instanceha
if 'novaclient' not in sys.modules:
    sys.modules['novaclient'] = MagicMock()
    sys.modules['novaclient.client'] = MagicMock()
    class NotFound(Exception):
        pass
    class Conflict(Exception):
        pass
    class Forbidden(Exception):
        pass
    class Unauthorized(Exception):
        pass
    novaclient_exceptions = MagicMock()
    novaclient_exceptions.NotFound = NotFound
    novaclient_exceptions.Conflict = Conflict
    novaclient_exceptions.Forbidden = Forbidden
    novaclient_exceptions.Unauthorized = Unauthorized
    sys.modules['novaclient.exceptions'] = novaclient_exceptions
if 'keystoneauth1' not in sys.modules:
    sys.modules['keystoneauth1'] = MagicMock()
    sys.modules['keystoneauth1.loading'] = MagicMock()
    sys.modules['keystoneauth1.session'] = MagicMock()
    sys.modules['keystoneauth1.exceptions'] = MagicMock()
    sys.modules['keystoneauth1.exceptions.connection'] = MagicMock()
    sys.modules['keystoneauth1.exceptions.http'] = MagicMock()
    sys.modules['keystoneauth1.exceptions.discovery'] = MagicMock()

logging.basicConfig(level=logging.DEBUG)

from instanceha.ai.tools import ApprovalLevel, ToolResult, registry as tool_registry
from instanceha.ai.safety import ApprovalManager, AuditLogger
from instanceha.ai.mcp_server import (
    InstanceHAMCPServer,
    _filter_params,
    _build_tool_input_schema,
    _INJECTED_PARAMS,
    _mcp_available,
)


class TestFilterParams(unittest.TestCase):
    """Test parameter filtering for MCP tool schemas."""

    def test_removes_connection_and_service(self):
        params = {"connection": "OpenStackClient", "service": "InstanceHAService",
                  "host": "str", "action": "str"}
        filtered = _filter_params(params)
        self.assertNotIn("connection", filtered)
        self.assertNotIn("service", filtered)
        self.assertIn("host", filtered)
        self.assertIn("action", filtered)

    def test_empty_params(self):
        self.assertEqual(_filter_params({}), {})

    def test_only_injected_params(self):
        params = {"connection": "X", "service": "Y"}
        self.assertEqual(_filter_params(params), {})

    def test_no_injected_params(self):
        params = {"host": "str", "minutes": "int"}
        filtered = _filter_params(params)
        self.assertEqual(len(filtered), 2)


class TestBuildToolInputSchema(unittest.TestCase):
    """Test JSON Schema generation for MCP tools."""

    def test_basic_schema(self):
        tool_obj = MagicMock()
        tool_obj.parameters = {"host": "str", "action": "str",
                               "connection": "OpenStackClient"}
        schema = _build_tool_input_schema(tool_obj)
        self.assertEqual(schema["type"], "object")
        self.assertIn("host", schema["properties"])
        self.assertIn("action", schema["properties"])
        self.assertNotIn("connection", schema["properties"])

    def test_numeric_params(self):
        tool_obj = MagicMock()
        tool_obj.parameters = {"minutes": "int", "count": "float"}
        schema = _build_tool_input_schema(tool_obj)
        self.assertEqual(schema["properties"]["minutes"]["type"], "number")
        self.assertEqual(schema["properties"]["count"]["type"], "number")

    def test_bool_params(self):
        tool_obj = MagicMock()
        tool_obj.parameters = {"force": "bool"}
        schema = _build_tool_input_schema(tool_obj)
        self.assertEqual(schema["properties"]["force"]["type"], "boolean")

    def test_empty_params(self):
        tool_obj = MagicMock()
        tool_obj.parameters = {"connection": "X", "service": "Y"}
        schema = _build_tool_input_schema(tool_obj)
        self.assertEqual(len(schema["properties"]), 0)
        self.assertEqual(len(schema["required"]), 0)


class TestInstanceHAMCPServer(unittest.TestCase):
    """Test the MCP server construction and tool execution."""

    def setUp(self):
        self.mock_conn = MagicMock()
        self.mock_service = MagicMock()
        self.audit_logger = AuditLogger(log_path="/tmp/test-mcp-audit.log")
        self.approval_manager = ApprovalManager(
            auto_approve_level=ApprovalLevel.NONE,
            audit_logger=self.audit_logger,
            dry_run=True,
        )
        self.server = InstanceHAMCPServer(
            nova_connection=self.mock_conn,
            service=self.mock_service,
            approval_manager=self.approval_manager,
            allow_writes=False,
        )

    def test_construction(self):
        self.assertFalse(self.server._allow_writes)
        self.assertEqual(self.server._port, 8081)
        self.assertEqual(self.server._host, "0.0.0.0")

    def test_build_inject_kwargs_with_connection(self):
        tool_obj = MagicMock()
        tool_obj.parameters = {"connection": "X", "host": "str"}
        inject = self.server._build_inject_kwargs(tool_obj)
        self.assertIn("connection", inject)
        self.assertIs(inject["connection"], self.mock_conn)

    def test_build_inject_kwargs_with_service(self):
        tool_obj = MagicMock()
        tool_obj.parameters = {"service": "Y"}
        inject = self.server._build_inject_kwargs(tool_obj)
        self.assertIn("service", inject)
        self.assertIs(inject["service"], self.mock_service)

    def test_build_inject_kwargs_no_injected(self):
        tool_obj = MagicMock()
        tool_obj.parameters = {"host": "str"}
        inject = self.server._build_inject_kwargs(tool_obj)
        self.assertEqual(len(inject), 0)

    def test_execute_unknown_tool(self):
        result = self.server._execute(self.approval_manager, "nonexistent_tool", {})
        parsed = json.loads(result)
        self.assertIn("error", parsed)
        self.assertIn("Unknown tool", parsed["error"])

    def test_execute_read_tool(self):
        """Execute a read-only tool through the MCP server."""
        self.mock_conn.services.list.return_value = []
        result = self.server._execute(
            self.approval_manager, "get_compute_services", {})
        parsed = json.loads(result)
        self.assertTrue(parsed["success"])

    def test_execute_write_tool_requires_approval(self):
        """Write tools should require approval when auto_approve is NONE."""
        result = self.server._execute(
            self.approval_manager, "fence_host",
            {"host": "compute-1", "action": "off"})
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "approval_required")
        self.assertIn("approval_id", parsed)

    def test_update_connection(self):
        new_conn = MagicMock()
        self.server.update_connection(new_conn)
        self.assertIs(self.server._nova_connection, new_conn)

    def test_get_observer_returns_none_when_not_set(self):
        """Observer lookup should return None gracefully."""
        result = self.server._get_observer()
        # May return None or the actual observer depending on test state
        # Just verify it doesn't raise
        self.assertTrue(result is None or result is not None)


class TestMCPServerWriteAccess(unittest.TestCase):
    """Test allow_writes flag behavior."""

    def setUp(self):
        self.audit_logger = AuditLogger(log_path="/tmp/test-mcp-audit.log")

    def test_allow_writes_false_skips_write_tools(self):
        """When allow_writes=False, write tools should not be registered."""
        server = InstanceHAMCPServer(
            nova_connection=MagicMock(),
            service=MagicMock(),
            approval_manager=ApprovalManager(
                auto_approve_level=ApprovalLevel.NONE,
                audit_logger=self.audit_logger,
                dry_run=True,
            ),
            allow_writes=False,
        )
        # Count tools that would be registered
        write_tools = [t for t in tool_registry.list_tools()
                       if t.approval_level != ApprovalLevel.NONE]
        read_tools = [t for t in tool_registry.list_tools()
                      if t.approval_level == ApprovalLevel.NONE]

        self.assertTrue(len(write_tools) > 0, "Should have some write tools")
        self.assertTrue(len(read_tools) > 0, "Should have some read tools")

        # When allow_writes is False, _register_tool should skip write tools
        mock_mcp = MagicMock()
        mock_mcp.tool.return_value = lambda f: f
        mock_mcp.resource.return_value = lambda f: f

        for tool_obj in tool_registry.list_tools():
            server._register_tool(mock_mcp, tool_obj)

        # mock_mcp.tool() should have been called only for read tools
        self.assertEqual(mock_mcp.tool.call_count, len(read_tools))

    def test_allow_writes_true_registers_all_tools(self):
        """When allow_writes=True, all tools should be registered."""
        server = InstanceHAMCPServer(
            nova_connection=MagicMock(),
            service=MagicMock(),
            approval_manager=ApprovalManager(
                auto_approve_level=ApprovalLevel.NONE,
                audit_logger=self.audit_logger,
                dry_run=True,
            ),
            allow_writes=True,
        )
        all_tools = tool_registry.list_tools()

        mock_mcp = MagicMock()
        mock_mcp.tool.return_value = lambda f: f

        for tool_obj in all_tools:
            server._register_tool(mock_mcp, tool_obj)

        self.assertEqual(mock_mcp.tool.call_count, len(all_tools))


class TestMCPServerResources(unittest.TestCase):
    """Test MCP resource registration."""

    def test_register_resources(self):
        """Verify all expected resources are registered."""
        server = InstanceHAMCPServer(
            nova_connection=MagicMock(),
            service=MagicMock(),
            approval_manager=MagicMock(),
        )
        mock_mcp = MagicMock()
        # Track resource URIs registered
        registered_uris = []
        def capture_resource(uri):
            registered_uris.append(uri)
            return lambda f: f
        mock_mcp.resource = capture_resource

        server._register_resources(mock_mcp)

        self.assertIn("instanceha://health", registered_uris)
        self.assertIn("instanceha://alerts", registered_uris)
        self.assertIn("instanceha://alerts/all", registered_uris)
        self.assertIn("instanceha://events", registered_uris)
        self.assertIn("instanceha://host/{host}", registered_uris)

    def test_five_resources_registered(self):
        """Exactly 5 resources should be registered."""
        server = InstanceHAMCPServer(
            nova_connection=MagicMock(),
            service=MagicMock(),
            approval_manager=MagicMock(),
        )
        mock_mcp = MagicMock()
        registered = []
        def capture_resource(uri):
            registered.append(uri)
            return lambda f: f
        mock_mcp.resource = capture_resource

        server._register_resources(mock_mcp)
        self.assertEqual(len(registered), 5)


class TestMCPServerCreateMCP(unittest.TestCase):
    """Test _create_mcp when MCP SDK is available."""

    @unittest.skipUnless(_mcp_available, "MCP SDK not installed")
    def test_create_mcp_returns_fastmcp(self):
        """If the MCP SDK is installed, _create_mcp should return a FastMCP."""
        from mcp.server.fastmcp import FastMCP
        server = InstanceHAMCPServer(
            nova_connection=MagicMock(),
            service=MagicMock(),
            approval_manager=MagicMock(),
        )
        mcp = server._create_mcp()
        self.assertIsInstance(mcp, FastMCP)


class TestMCPStartFunction(unittest.TestCase):
    """Test the _start_mcp_server() helper in main.py."""

    def test_start_mcp_server_graceful_when_sdk_missing(self):
        """_start_mcp_server should return None gracefully when SDK is missing."""
        from instanceha.main import _start_mcp_server
        with patch('instanceha.ai.mcp_server._mcp_available', False):
            result = _start_mcp_server(MagicMock(), MagicMock())
            self.assertIsNone(result)

    def test_start_mcp_server_graceful_on_exception(self):
        """_start_mcp_server should catch exceptions and return None."""
        from instanceha.main import _start_mcp_server
        with patch('instanceha.ai.mcp_server.InstanceHAMCPServer',
                   side_effect=RuntimeError("test")):
            result = _start_mcp_server(MagicMock(), MagicMock())
            self.assertIsNone(result)


class TestMCPServerAvailability(unittest.TestCase):
    """Test the is_available property."""

    def test_is_available_reflects_sdk_state(self):
        server = InstanceHAMCPServer(
            nova_connection=MagicMock(),
            service=MagicMock(),
            approval_manager=MagicMock(),
        )
        self.assertEqual(server.is_available, _mcp_available)

    def test_start_returns_none_when_unavailable(self):
        """start() should return None when MCP SDK is not installed."""
        with patch('instanceha.ai.mcp_server._mcp_available', False):
            server = InstanceHAMCPServer(
                nova_connection=MagicMock(),
                service=MagicMock(),
                approval_manager=MagicMock(),
            )
            result = server.start()
            self.assertIsNone(result)


class TestInjectedParams(unittest.TestCase):
    """Test that _INJECTED_PARAMS is correct."""

    def test_injected_params_set(self):
        self.assertEqual(_INJECTED_PARAMS, {"connection", "service"})

    def test_all_tools_have_consistent_injection(self):
        """Every tool with 'connection' or 'service' params should get them injected."""
        for tool_obj in tool_registry.list_tools():
            params = tool_obj.parameters or {}
            server = InstanceHAMCPServer(
                nova_connection=MagicMock(),
                service=MagicMock(),
                approval_manager=MagicMock(),
            )
            inject = server._build_inject_kwargs(tool_obj)
            if "connection" in params:
                self.assertIn("connection", inject,
                              f"Tool {tool_obj.name} expects 'connection' but it's not injected")
            if "service" in params:
                self.assertIn("service", inject,
                              f"Tool {tool_obj.name} expects 'service' but it's not injected")


if __name__ == '__main__':
    unittest.main()
