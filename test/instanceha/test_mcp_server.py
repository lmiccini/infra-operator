"""Tests for MCP Server integration."""

import json
import logging
import sys
import unittest
from unittest.mock import MagicMock

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

import os
instanceha_path = os.path.join(os.path.dirname(__file__), '..', '..', 'templates', 'instanceha', 'bin')
sys.path.insert(0, os.path.abspath(instanceha_path))

logging.basicConfig(level=logging.DEBUG)

from instanceha_ai.tools import ApprovalLevel, ToolResult, registry as tool_registry
from instanceha_ai.safety import ApprovalManager, AuditLogger
from instanceha_ai.mcp_server import (
    InstanceHAMCPServer,
    _filter_params,
    _INJECTED_PARAMS,
    _mcp_available,
)


class TestFilterParams(unittest.TestCase):
    def test_removes_connection_and_service(self):
        params = {"connection": "OpenStackClient", "service": "InstanceHAService",
                  "host": "str", "action": "str"}
        filtered = _filter_params(params)
        self.assertNotIn("connection", filtered)
        self.assertNotIn("service", filtered)
        self.assertIn("host", filtered)

    def test_empty_params(self):
        self.assertEqual(_filter_params({}), {})

    def test_only_injected_params(self):
        self.assertEqual(_filter_params({"connection": "X", "service": "Y"}), {})


class TestInstanceHAMCPServer(unittest.TestCase):
    def setUp(self):
        self.mock_conn = MagicMock()
        self.mock_service = MagicMock()
        self.audit_logger = AuditLogger(log_path="/tmp/test-mcp-audit.log")
        self.approval_manager = ApprovalManager(
            auto_approve_level=ApprovalLevel.NONE,
            audit_logger=self.audit_logger, dry_run=True,
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

    def test_build_inject_kwargs_with_connection(self):
        tool_obj = MagicMock()
        tool_obj.parameters = {"connection": "X", "host": "str"}
        inject = self.server._build_inject_kwargs(tool_obj)
        self.assertIn("connection", inject)

    def test_build_inject_kwargs_with_service(self):
        tool_obj = MagicMock()
        tool_obj.parameters = {"service": "Y"}
        inject = self.server._build_inject_kwargs(tool_obj)
        self.assertIn("service", inject)

    def test_execute_unknown_tool(self):
        result = self.server._execute(self.approval_manager, "nonexistent_tool", {})
        parsed = json.loads(result)
        self.assertIn("error", parsed)

    def test_execute_read_tool(self):
        self.mock_conn.services.list.return_value = []
        result = self.server._execute(
            self.approval_manager, "get_compute_services", {})
        parsed = json.loads(result)
        self.assertTrue(parsed["success"])

    def test_execute_write_tool_requires_approval(self):
        result = self.server._execute(
            self.approval_manager, "fence_host",
            {"host": "compute-1", "action": "off"})
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "approval_required")

    def test_update_connection(self):
        new_conn = MagicMock()
        self.server.update_connection(new_conn)
        self.assertIs(self.server._nova_connection, new_conn)


class TestMCPServerWriteAccess(unittest.TestCase):
    def setUp(self):
        self.audit_logger = AuditLogger(log_path="/tmp/test-mcp-audit.log")

    def test_allow_writes_false_skips_write_tools(self):
        server = InstanceHAMCPServer(
            nova_connection=MagicMock(), service=MagicMock(),
            approval_manager=ApprovalManager(
                auto_approve_level=ApprovalLevel.NONE,
                audit_logger=self.audit_logger, dry_run=True),
            allow_writes=False,
        )
        read_tools = [t for t in tool_registry.list_tools()
                      if t.approval_level == ApprovalLevel.NONE]
        mock_mcp = MagicMock()
        mock_mcp.tool.return_value = lambda f: f
        for tool_obj in tool_registry.list_tools():
            server._register_tool(mock_mcp, tool_obj)
        self.assertEqual(mock_mcp.tool.call_count, len(read_tools))

    def test_allow_writes_true_registers_all_tools(self):
        server = InstanceHAMCPServer(
            nova_connection=MagicMock(), service=MagicMock(),
            approval_manager=ApprovalManager(
                auto_approve_level=ApprovalLevel.NONE,
                audit_logger=self.audit_logger, dry_run=True),
            allow_writes=True,
        )
        all_tools = tool_registry.list_tools()
        mock_mcp = MagicMock()
        mock_mcp.tool.return_value = lambda f: f
        for tool_obj in all_tools:
            server._register_tool(mock_mcp, tool_obj)
        self.assertEqual(mock_mcp.tool.call_count, len(all_tools))


class TestMCPServerResources(unittest.TestCase):
    def test_register_resources(self):
        server = InstanceHAMCPServer(
            nova_connection=MagicMock(), service=MagicMock(),
            approval_manager=MagicMock(),
        )
        mock_mcp = MagicMock()
        registered_uris = []
        def capture_resource(uri):
            registered_uris.append(uri)
            return lambda f: f
        mock_mcp.resource = capture_resource
        server._register_resources(mock_mcp)
        self.assertIn("instanceha://health", registered_uris)
        self.assertIn("instanceha://alerts", registered_uris)
        self.assertIn("instanceha://events", registered_uris)
        self.assertEqual(len(registered_uris), 5)


class TestMCPServerAvailability(unittest.TestCase):
    def test_is_available_reflects_sdk_state(self):
        server = InstanceHAMCPServer(
            nova_connection=MagicMock(), service=MagicMock(),
            approval_manager=MagicMock(),
        )
        self.assertEqual(server.is_available, _mcp_available)


class TestInjectedParams(unittest.TestCase):
    def test_injected_params_set(self):
        self.assertEqual(_INJECTED_PARAMS, {"connection", "service"})

    def test_all_tools_have_consistent_injection(self):
        for tool_obj in tool_registry.list_tools():
            params = tool_obj.parameters or {}
            server = InstanceHAMCPServer(
                nova_connection=MagicMock(), service=MagicMock(),
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
